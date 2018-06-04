import os
import numpy as np
from numba import jit
from multiprocessing import Pool
from spiketag.view import wave_view 
from .SPK import SPK
from .Binload import bload
from ..utils.conf import info

def _calculate_threshold(x, beta=4.0):
    thr = -beta*np.median(abs(x)/0.6745,axis=0)
    return thr

@jit(cache=True, nopython=True)
def _to_spk(data, pos, chlist, spklen=19, prelen=8):
    n = len(pos)
    spk = np.empty((n, spklen, len(chlist)), dtype=np.float32)
    for i in range(n):
        # i spike in chlist
        spk[i, ...]  = data[pos[i]-prelen+2:pos[i]-prelen+spklen+2, chlist]
    _nan = np.where(chlist==-1)[0]
    spk[..., _nan] = 0
    return spk

class MUA():
    def __init__(self, mua_filename, spk_filename, probe, numbytes=4, binary_radix=13):
        
        self.nCh = probe.n_ch
        self.fs  = probe.fs*1.0
        self.probe = probe
        self.numbytes = numbytes
        self.dtype = 'i'+str(self.numbytes)
        self.bf = bload(self.nCh, self.fs)
        self.bf.load(mua_filename, dtype=self.dtype)
        self.mua_file = mua_filename
        if probe.reorder_by_chip is True:
            self.bf.reorder_by_chip(probe._nchips)
        self.data = self.bf.asarray(binpoint=binary_radix)
        self.scale = self.data.max() - self.data.min()
        self.t    = self.bf.t
        self.npts = self.bf._npts
        self.spklen = 19
        self.prelen = 9 

        # acquire pivotal_pos from spk.bin under same folder
        foldername = '/'.join(self.mua_file.split('/')[:-1])+'/'
        info('processing folder: {}'.format(foldername))
        # self.spk_file = self.mua_file[:-4] + '.spk.bin'
        self.spk_file = spk_filename
        spk_meta = np.fromfile(self.spk_file, dtype='<i4')
        self.pivotal_pos = spk_meta.reshape(-1,2).T

        # check spike is extracable
        # delete begin AND end
        self.pivotal_pos = np.delete(self.pivotal_pos, 
                           np.where((self.pivotal_pos[0] + self.spklen) > self.data.shape[0])[0], axis=1)

        self.pivotal_pos = np.delete(self.pivotal_pos, 
                           np.where((self.pivotal_pos[0] - self.prelen) < 0)[0], axis=1)        

        info('raw data have {} spks'.format(self.pivotal_pos.shape[1]))
        info('----------------success------------------')
        info(' ')

    def get_threshold(self):
        return _calculate_threshold(self.data[::100])

    def tofile(self, file_name, nchs, dtype=np.int32):
        data = self.data[:, nchs].astype(dtype)
        data.tofile(file_name)

    def tospk(self):
        info('mua.tospk()')
        spkdict = {}
        for g in self.probe.grp_dict.keys():
            # pivotal_chs = self.probe.fetch_pivotal_chs(g)
            pivotal_chs = self.probe.grp_dict[g]
            pos = self.pivotal_pos[0][np.in1d(self.pivotal_pos[1], pivotal_chs)]
            if len(pos) > 0:
                spkdict[g] = _to_spk( data   = self.data, 
                                      pos    = pos, 
                                      chlist = self.probe[g], 
                                      spklen = self.spklen,
                                      prelen = self.prelen)
        info('----------------success------------------')     
        info(' ')               
        return SPK(spkdict)

    def get_nid(self, corr_cutoff=0.95):  # get noisy spk id
        # 1. dump spikes file (binary)
        piv = self.pivotal_pos.T
        nspk = self.pivotal_pos.shape[1]
        # the reason adding mod operation here is if the spike is in the very end ,i.e: within 15 offset to end 
        # point, this will make self.data[rows, :] out of bound.
        rows = (np.arange(-10,15).reshape(1,-1) + piv[:,0].reshape(-1,1)) % self.data.shape[0]
        cols = piv[:,1].reshape(-1,1)
        full_spk = self.data[rows, :]
        filename = os.path.dirname(self.filename)+'/.'+os.path.basename(self.filename)+'.spkfull'
        full_spk.tofile(filename)

        # 2. parallel screen the noise id out, then gather from CPUs
        from ipyparallel import Client
        from ipyparallel.util import interactive
        rc = Client()
        cpu = rc[:]
        cpu.block = True

        @cpu.remote(block=True)      # to be executed by cpu
        @interactive                 # to be on the global()
        def get_noise_ids(filename, corr_cutoff, n_group):
            spk_data = np.memmap(filename, dtype='f4').reshape(-1, 25, n_group)
            noise_id = []
            # corr_cutoff = 0.98
            # ind is index assign to each cpu
            # corr_cutoff is threshold of corr_coef
            for i in ind:
                spikes = spk_data[i]
                seq = np.abs(np.corrcoef(spikes[5:15, 16:].T).ravel())
                seq[seq>corr_cutoff] = 1
                seq[seq<corr_cutoff] = 0
                if np.median(seq) == 1.0:
                    noise_id.append(i)
            return noise_id

        # f = interactive(get_noise_ids)
        cpu.execute('import numpy as np')
        cpu.scatter('ind', range(nspk))
        noise_id = get_noise_ids(filename, corr_cutoff, self.probe.n_group)
        # cpu.execute("%reset")
        try:
            os.remove(filename)
        except OSError:
            pass
        return np.hstack(np.asarray(noise_id))


    def remove_high_corr_noise(self, corr_cutoff=0.95):
        nid = self.get_nid(corr_cutoff)
        self.pivotal_pos = np.delete(self.pivotal_pos, nid, axis=1)
        info('removed noise ids: {} '.format(nid)) 

    def remove_groups_under_fetlen(self, fetlen):
        ids = []
        groups = {}
        for g in range(self.probe.n_group):
            pivotal_chs = self.probe.fetch_pivotal_chs(g)
            _ids = np.where(np.in1d(self.pivotal_pos[1], pivotal_chs))[0]
            if len(_ids) < fetlen:
                ids.extend(_ids)
                groups[g] = len(_ids)
        self.pivotal_pos = np.delete(self.pivotal_pos, ids, axis=1)
        info('removed all spks on these groups: {}'.format(groups)) 

    def group_spk_times(self):
        group_with_times = {}
        for g in range(self.probe.n_group):
            times = self.pivotal_pos[0][np.where(np.in1d(self.pivotal_pos[1],self.probe[g]))[0]]
            if len(times) > 0: group_with_times[g] = times
        return group_with_times

    def show(self, chs):
        wview = wave_view(self.data, chs=chs, spks=self.pivotal_pos)
        wview.show()