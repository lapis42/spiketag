import numpy as np

#------------------------------
#        Probe Factory 
#------------------------------

class ProbeFactory(object):
    ''' probe factory: 
            generate the probe by spicifying the type of probe.

        type
        ------
        LinearProbe:
                fs : float
                    sample rate
                n_ch : int
                    number of channel
                ch_span : int
                    cross up and down ch_span channel respectly 
        
        TetrodeProbe:
                fs : float 
                    sample rate
                n_ch : int
                    number of channel
                gmaps: dist
                    the map from goup to channel, i.e: {1:[1,2,3,4]}. if gmaps is None, defaut sequence maps will be applied.   
    '''
    @staticmethod
    def genLinearProbe(fs, n_ch, ch_span=1):
        return LinearProbe(fs, n_ch, ch_span)

    @staticmethod 
    def genTetrodeProbe(fs, n_ch, gmaps=None):
        if not gmaps:
            gmaps = {}
            for g in range(n_ch/4):
               gmaps[g] = range(g*4, g*4+4)
        else:
            assert len(gmaps.values()) == n_ch, 'Total number of chs should be equals.'
        return TetrodeProbe(fs, n_ch, gmaps)

# -------------------------------
#         Type of Probes
# -------------------------------

class Probe(object):
    ''' the base class of probe
    '''
    def __init__(self, type):
        assert type is not None
        self._type = type

    @property
    def type(self):
        '''
          the string of type, some place need to distinguish the type 
        '''
        return self._type

    @property
    def fs(self):
        return self._fs
    
    @property
    def n_ch(self):
        return self._n_ch

    @property
    def n_group(self):
        '''
          number of groups, group can be treated as basic unit to statistic data
        '''
        return self._n_group
    
    @property
    def groups(self):
        '''
            return all group numbers
        '''
        for i in range(self._n_group):
            yield i

    @property
    def len_group(self):
        '''
            number of channles in a group
        '''
        return self._len_group

class LinearProbe(Probe):
    ''' linear probe
    '''
    def __init__(self, fs, n_ch, ch_span):
        super(LinearProbe, self).__init__('linear')
        
        assert fs > 0 
        assert n_ch > 0
        assert ch_span >0 and ch_span < n_ch

        self._fs = fs
        self._n_ch = n_ch
        self._ch_span = ch_span
        self._n_group = self._n_ch
        self._len_group = 2 * ch_span + 1
   
    @property
    def ch_span(self):
        return self._ch_span

    # -------------------------------
    #        public method 
    # -------------------------------

    def get_group(self, ch):
        ''' 
            get all the chs in group which the ch be belonged
        '''
        assert ch >= 0 and ch < self._n_ch

        chmax = self._n_ch - 1
        start = ch - self._ch_span # if ch-span>=0 else 0
        end   = ch + self._ch_span # if ch+span<chmax else chmax
        near_ch = np.arange(start, end+1, 1)
        near_ch[near_ch>chmax] = -1
        near_ch[near_ch<0] = -1
        return near_ch
    
    def belong_group(self, ch):
        '''
            get the group number which ch belong
        '''
        assert ch >= 0 and ch < self._n_ch
        return ch

    def get_chs(self, group):
        '''
            get the chs which group has
        '''
        assert group >= 0 and group < self._n_group
        return self.get_group(group)

    def fetch_pivotal_chs(self, group):
        '''
            get the most important chs within group
        '''
        assert group >= 0 and group < self._n_group

        chs = self.get_group(group)
        return np.asarray([chs[len(chs)/2]])

class TetrodeProbe(Probe):
    ''' tetrode probe
    '''
    def __init__(self, fs, n_ch, g2chs):
        super(TetrodeProbe, self).__init__('tetrode')

        assert fs > 0
        assert n_ch > 0 and n_ch % 4 == 0
        assert g2chs

        self._fs = fs
        self._n_ch = n_ch
        self._n_group = int(self._n_ch / 4)
        self._len_group = 4
        self._g2chs = g2chs
        self._ch2g = {}
        for g, chs in self._g2chs.items():
            self._update_chs2group(chs, g)
            
    # -------------------------------
    #        public method 
    # -------------------------------

    def get_chs(self, group):
        '''
            get the chs which group has
        '''
        assert group >= 0 and group < self._n_group
        return self._g2chs[group] 

    def belong_group(self, ch):
        '''
            return the group number which ch is belonged
        '''
        assert ch >= 0 and ch < self._n_ch
        return self._ch2g[ch]

    def fetch_pivotal_chs(self, group):
        '''
            get the most important chs within group
        '''
        assert group >= 0 and group < self._n_group
        return self._g2chs[group]
    
    def _update_chs2group(self, chs, g):
       for ch in chs:
            self._ch2g[ch] = g

    def __getitem__(self, group):
        return self._g2chs(group)

    def __setitem__(self, group, chs):
        assert group >= 0 and group < self._n_group
        assert len(chs) == 4
        self._g2chs[group] = chs
        self._update_chs2group(chs, group)

    def __str__(self):
        return '\n'.join(['{}:{}'.format(key, val) for key, val in self._g2chs.items()]) 

    __repr__ = __str__
