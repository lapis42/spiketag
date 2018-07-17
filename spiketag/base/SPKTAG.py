from .MUA import MUA
from .SPK import SPK
from .FET import FET
from .CLU import CLU
import numpy as np
import json
import pickle


class SPKTAG(object):
    def __init__(self, probe=None, spk=None, fet=None, clu=None, gtimes=None, filename=None):
        '''
        spk    : spk object
        fet    : fet object
        clu    : dictionary of clu object (each item is a channel based clu object)
        gtimes : dictionary of group with spike times
        '''
        self.probe = probe
        if filename is not None: # load from file
            self.fromfile(filename)
        elif gtimes is not None : # construct
            self.gtimes  = gtimes
            self.spk     = spk 
            self.fet     = fet 
            self.clu     = clu 
            self.spklen  = spk.spklen
            self.fetlen  = fet.fetlen
            self.grplen  = self.probe.group_len
            self.ngrp    = len(self.probe.grp_dict.keys())

            self.dtype   = [('t', 'int32'),
                            ('group','int32'),  
                            ('spk', 'f4', (self.spklen, self.grplen)), 
                            ('fet','f4',(self.fetlen,)),
                            ('clu','int32')]
        else:
            pass

    @property
    def nspk(self):
        return sum([len(v) for v in self.gtimes.values()])

    def build_meta(self):
        meta = {}
        meta["ngrp"] = self.ngrp
        meta["grplen"] = self.probe.group_len
        meta["fetlen"] = self.fetlen
        meta["spklen"] = self.spklen
        return meta

    def build_hdbscan_tree(self):
        treeinfo = {}
        for i in range(self.ngrp):
            treeinfo[i] = self.clu[i]._extra_info
        return treeinfo

    def build_spktag(self):
        spktag = np.zeros(self.nspk, dtype=self.dtype)
        start_index = 0
        for g, times in self.gtimes.items():
            end_index = start_index + len(times)
            spktag['t'][start_index:end_index] = times
            spktag['group'][start_index:end_index] = np.full((len(times)), g, dtype=np.int)
            spktag['spk'][start_index:end_index] = self.spk[g]
            spktag['fet'][start_index:end_index] = self.fet[g]        
            spktag['clu'][start_index:end_index] = self.clu[g].membership
            start_index = end_index
        return spktag


    def update(self, spk, fet, clu, gtimes):
        self.spk = spk
        self.fet = fet
        self.clu = clu
        self.gtimes = gtimes
        self.build_meta()
        self.build_spktag()	


    def tofile(self, filename):
        self.meta = self.build_meta()
        self.treeinfo = self.build_hdbscan_tree()
        self.spktag = self.build_spktag()
        with open(filename+'.meta', 'w') as metafile:
                json.dump(self.meta, metafile, indent=4)
        np.save(filename+'.npy', self.treeinfo)
        self.spktag.tofile(filename)


    def fromfile(self, filename):
        # meta file
        with open(filename+'.meta', 'r') as metafile:
            self.meta = json.load(metafile)
        self.ngrp   = self.meta['ngrp']
        self.grplen = self.meta['grplen']
        self.spklen = self.meta['spklen']
        self.fetlen = self.meta['fetlen']

        # condensed tree info
        self.treeinfo = np.load(filename+'.npy').item()

        # spiketag
        self.dtype = [('t', 'int32'), 
                      ('group', 'int32'),  
                      ('spk', 'f4', (self.spklen, self.grplen)), 
                      ('fet', 'f4',(self.fetlen,)),
                      ('clu', 'int32')]
        self.spktag = np.fromfile(filename, dtype=self.dtype)


    def tospk(self):
        spkdict = {}
        for g in self.gtimes.keys():
            spkdict[g] = self.spktag['spk'][self.spktag['group']==g]
        self.spk = SPK(spkdict)
        return self.spk		


    def tofet(self):
        fetdict = {}
        for g in self.gtimes.keys():
            fetdict[g] = self.spktag['fet'][self.spktag['group']==g]
        self.fet = FET(fetdict)
        return self.fet		


    def toclu(self):
        cludict = {}
        for g in self.gtimes.keys():
            cludict[g] = CLU(self.spktag['clu'][self.spktag['group']==g], treeinfo=self.treeinfo[g])
        self.clu = cludict
        return self.clu


    def to_gtimes(self):
        times = self.spktag['t']
        groups = self.spktag['group']
        gtimes = {}
        for g in np.unique(groups):
            gtimes[g] = times[np.where(groups == g)[0]]
        self.gtimes = gtimes
        return self.gtimes
        
