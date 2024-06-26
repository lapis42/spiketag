import numpy as np
import pandas as pd
from sympy import binomial_coefficients
from .SPK import SPK
from .FET import FET
from .CLU import CLU
from ..view import scatter_3d_view, grid_scatter3d, raster_view


class UNIT(object):
    """
    UNIT class load, visualize and analyze unit structured data
    unit structure in which each unit is: {time, group_id, fet0, fet1, fet2, fet3, spike_id}, each section uses a 32-bit integer to represent.

    - UNIT class bins the unit data.
    - UNIT class convert the feature part of the unit data to Spike FET, which can call sorting, and spiketag feature view. 

    Inputs:
        bin_len: float (s) of bin length
        nbins: int of number of bins
        binpoint: bits used to encode the interger part of a 32-bit number (default 13)


    Usage: make sure that `spk_wav.bin` and `fet.bin` are in the same directory.
    >>> from spiketag.base import *
    >>> unit = UNIT()
    >>> unit.load_ephys()

    """
    def __init__(self, bin_len=0.1, nbins=8, binpoint=13, sampling_rate=25000.0):
        super(UNIT, self).__init__()
        self._bin_len = bin_len
        self._nbins   = nbins
        self.binpoint = binpoint
        self.sampling_rate = sampling_rate

    def load_ephys(self, n_items=8):
        self.spk = SPK()
        self.spk.load_spkwav()
        self.load_unitpacket('./fet.bin', n_items=n_items)

    def load_playground_log(self, log_filename):
        '''
        This method is specific to playground user
        '''
        pass

    def load_unitpacket(self, filename, n_items=8):
        '''
        1. pd dataframe
        2. fet.bin

        Both follows table structure:
        when n_items=7:
        ['time', 'group_id', 'fet0', 'fet1', 'fet2', 'fet3', 'spike_id']

        when n_items=8:
        ['time', 'group_id', 'fet0', 'fet1', 'fet2', 'fet3', 'spike_id', 'mean_spk_range']
        '''
        self.filename = filename
        if filename.split('.')[-1]=='pd':
            self.df = pd.read_pickle(filename)
            self.df['frame_id'] /= self.sampling_rate
            self.df.rename(columns={'frame_id':'time'}, inplace=True)
            self.df['group_id'] = self.df['group_id'].astype('int')
            self.df['spike_id'] = self.df['spike_id'].astype('int')
            # self.df.set_index('spike_id', inplace=True)
            # self.df.index = self.df.index.astype(int)
            # self.df.index -= self.df.index.min()
            # self.df['spk'] = self.df
            # self.spk_time_dict = {i: self.df.loc[i]['time'].to_numpy() 
            #                       for i in self.df.index.unique().sort_values()}
            # self.df['spk'].reset_index(inplace=True)
            # self.n_units = np.sort(self.df.spike_id.unique()).shape[0]
            # self.n_groups = np.sort(self.df.group_id.unique()).shape[0]

        elif filename.split('.')[-1]=='bin':
            fet = np.fromfile(filename, dtype=np.int32).reshape(-1, n_items).astype(np.float32)
            fet[:, 2:6] /= float(2**self.binpoint)
            if n_items == 7:
                self.df = pd.DataFrame(fet,
                        columns=['time', 'group_id', 'fet0', 'fet1', 'fet2', 'fet3', 'spike_id'])
            elif n_items == 8:
                fet[:, -1] /= float(2**self.binpoint)
                fet[:, -1][fet[:, -1]<0] = 0.0
                self.df = pd.DataFrame(fet,
                        columns=['time', 'group_id', 'fet0', 'fet1', 'fet2', 'fet3', 'spike_id', 'spike_energy'])
            self.df['time'] /= self.sampling_rate
            self.df['group_id'] = self.df['group_id'].astype('int')
            self.df['spike_id'] = self.df['spike_id'].astype('int')

        error_idx = np.where(self.df.time.diff()<0)[0] # find the first point that time is not increasing
        if error_idx.shape[0] > 0:
            self.df = self.df.iloc[:error_idx[0]]

        self.labels = self.df.spike_id.sort_values().unique()

        self.assign_fet()
        self.assign_bin()

    def assign_fet(self):
        fet_dict = {}
        clu_dict = {}
        label = {}
        self.groups = self.df.group_id.sort_values().unique()
        self.n_grp = len(self.groups)
        for g in self.groups:
            fet_dict[g] = self.df[self.df.group_id==g][['fet0', 'fet1', 'fet2', 'fet3']].to_numpy()
            label[g] = self.df[self.df.group_id==g]['spike_id'].to_numpy()
            if len(label[g])>0:
                clu_dict[g] = CLU(label[g])
        self.fet = FET(fet_dict)
        self.fet.clu = clu_dict
        self.label = label

    def __repr__(self):
        return self.df.__repr__()

    @property
    def neuron_idx(self):
        return self.df.spike_id.unique()

    @property
    def n_units(self):
        return self.neuron_idx.shape[0]

    @property
    def spike_time(self):
        return self.df.time.to_numpy()

    @property
    def spike_id(self):
        return self.df.spike_id.to_numpy()

    @property
    def bin_ts(self):
        return self.bin_index*self.bin_len

    @property
    def bin_len(self):
        return self._bin_len
    
    @bin_len.setter
    def bin_len(self, value):
        self._bin_len = value
        self.assign_bin()

    @property
    def nbins(self):
        return self._nbins

    @nbins.setter
    def nbins(self, value):
        self._nbins = value
        self.assign_bin()

    @property
    def ts(self):
        t_step = self.bin_len
        ts = np.arange(self.df.bin_end_time.iloc[0] - t_step, self.df.bin_end_time.iloc[-1], t_step)
        return ts
    
    def get_scv(self, t_step=100e-3, start_time=None, end_time=None):
        '''
        when t_step=100e-3, this function should produce the same result as bmi_scv_full[:, -1, :], bmi_scv_full = np.fromfile('./scv.bin').reshape(-1, B_bins, n_units) 
        '''
        if start_time is None and end_time is None:
            start_time, end_time = self.df.bin_end_time.iloc[0], self.df.bin_end_time.iloc[-1]
        self.spk_time_dict = {i: self.df[ (self.df.spike_id == i) & 
                                          (self.df.time >  start_time - t_step) & 
                                          (self.df.time <= end_time) ].time.to_numpy() 
                              for i in np.sort(self.neuron_idx)} 
        ts = np.arange(start_time-t_step, end_time, t_step-1e-15)
        # self.scv = spk_time_to_scv(self.spk_time_dict, ts=ts, t_window = self.bin_len*self.nbins)
        self.scv = np.vstack([np.histogram(self.spk_time_dict[i], ts)[0] for i in np.arange(self.n_units)]).T
        return ts[1:], self.scv

    def to_spiketrain(self, start_time=0, end_time=None, neuron_idx=None, name=' '):
        if end_time is None:
            end_time = self.ts[-1]
            
        from spiketag.analysis import spike_train as ST
        if neuron_idx is None:
            spk = ST(self.spike_time[self.spike_id>0],  
                     self.spike_id[self.spike_id>0], name=name)
        else:
            spk = ST(self.spike_time[np.isin(self.spike_id, neuron_idx)], 
                     self.spike_id[np.isin(self.spike_id, neuron_idx)], name=name)
        return spk

    def assign_bin(self):
        '''
        critical function to 
        1. assign bin (and end time of each bin) to each spike
        2. assign bin_index to each bin
        '''
        # assign `bin` number to each spike 
        self.df['bin'] = self.df.time.apply(lambda x: int(x//self.bin_len))
        self.df['bin_end_time'] = self.df.time.apply(
            lambda x: (int(x//self.bin_len)+1)*self.bin_len)
        # assign bin_index to each bin (bin_index will be used to index scv.bin stored in BMI experiment)
        self.bin_index = self.df['bin'].unique() 
        if self.df.bin.min() != 0 and self.df.bin.iloc[0] < self._nbins:
            self.bin_index = np.insert(self.bin_index, 0, self._nbins-1) # insert 7 (if nbins==8) at 0 position
        # We still need to remove the first 7 bins (if nbins==8) and the last bin was never trigger the binner to send out command
        self.bin_index = self.bin_index[self._nbins-1:-1] 

    def show(self, g=0):
        self.current_group = g
        if g is None:
            self.gd = grid_scatter3d()
            self.gd.from_file(self.filename)
            self.gd.show()
        else:
            self.fet_view = scatter_3d_view()
            self.fet_view.show()
            self.fet_view.set_data(self.fet[g], self.fet.clu[g])
            self.fet_view.title = f'group {g}: {self.fet[g].shape[0]} spikes'

    def show_raster(self, unit_id='spike_id'):
        self.rasview = raster_view()
        # self.rasview.fromfile(self.filename)
        self.rasview.show()
        spkid_packet = self.df[['time', unit_id]].to_numpy()
        spkid_packet[:,0] *= self.sampling_rate
        self.rasview.set_data(spkid_packet.astype(np.int32))
        self.rasview.set_range()

    def highlight_low_energy_spike(self, threshold=0.3):
        '''
        specific to raster_view
        '''
        low_energy_idx = self.df[self.df.spike_energy < threshold].index.to_numpy()
        self.rasview.highlight(low_energy_idx)
