from .core import softmax, licomb_Matrix, bayesian_decoding, bayesian_decoding_rt, argmax_2d_tensor, smooth
import numpy as np
from sklearn.metrics import r2_score
from ..utils import plot_err_2d
import copy
import torch


def mua_count_cut_off(X, y=None, minimum_spikes=1):
    '''
    temporary solution to cut the frame that too few spikes happen

    X is the spike count vector(scv), (B_bins, N_neurons), the count in each bin is result from (t_window, t_step)
    minimum_spikes is the minimum number of spikes that allow the `bins`(rows) enter into the decoder
    '''
    for i in range(100):  # each iteration some low rate bins is removed
        mua_count = X.sum(axis=1) # sum over all neuron to get mua
        idx = np.where(mua_count<=minimum_spikes)[0]
        X[idx] = X[idx-1]
        if y is not None:
            y[idx] = y[idx-1]
    return X, y


def load_decoder(filename):
    # step 1: load the decoder from file
    dec = torch.load(filename)

    # step 2: assign place field to the place decoder
    # call pc.get_fields() first to update pc.fields and then transfer to dec.fields
    dec.pc.get_fields() # !critical to update pc.fields using this method first
    dec.fields = dec.pc.fields[1:]  # remove the first field, which is the 'noise'

    # step 3: store some reusable values in the decoder for fast online computing
    # cached value for real-time decoding on incoming bin (N units by B bins) from BMI
    dec.spatial_bin_size, dec.spatial_origin = dec.pc.bin_size, dec.pc.maze_original
    dec.poisson_matrix = dec.t_window*dec.fields.sum(axis=0) # one matrix reused in bayesian decoding
    dec.log_fr = np.log(dec.fields)  # log fields, make sure Fr[Fr==0] = 1e-12

    dec.partition(training_range=[0.0, 1.0], valid_range=[0.0, 1.0], testing_range=[0.0, 1.0], 
                  low_speed_cutoff={'training': True, 'testing': True})
    (X_train, y_train), (X_valid, y_valid), (X_test, y_test) = dec.get_data(minimum_spikes=2)
    predicted_y = dec.predict(X_test)
    smooth_factor  = int(2/dec.pc.t_step) # 2 second by default
    sm_predicted_y = smooth(predicted_y, smooth_factor)
    score = dec.r2_score(sm_predicted_y, y_test)
    print(f'decoder uses {dec.fields.shape[0]} neurons, R2 score: {score}')
    return dec

class Decoder(object):
    """Base class for the decoders for place prediction"""
    def __init__(self, t_window, t_step=None, verbose=True):
        '''
        t_window is the bin_size
        t_step   is the step_size (if None then use pc.ts as natrual sliding window)
        https://github.com/chongxi/spiketag/issues/47 
        
        For Non-RNN decoder, large bin size in a single bin are required
        For RNN decoder,   small bin size but multiple bins are required

        During certain neural state, such as MUA burst (ripple), a small step size is required 
        (e.g. t_window:20ms, t_step:5ms is used by Pfeiffer and Foster 2013 for trajectory events) 

        dec.partition(training_range, valid_range, testing_range, low_speed_cutoff) 
        serves the cross-validation
        https://github.com/chongxi/spiketag/issues/50
        '''
        self.t_window = t_window
        self.t_step   = t_step
        self.verbose  = verbose

    def connect_to(self, pc):
        '''
        This decoder is specialized for position decoding
        Connect to a place-cells object that contains behavior, neural data and co-analysis
        '''
        # self.pc = pc
        self.pc = copy.deepcopy(pc)
        self.pc.rank_fields('spatial_bit_spike') # rerank the field
        if self.t_step is not None:
            print('Link the decoder with the place cell object (pc):\r\n resample the pc according to current decoder input sampling rate {0:.4f} Hz'.format(1/self.t_step))
            self.pc(t_step=self.t_step)


    def resample(self, t_step=None, t_window=None):
        if t_window is None:
            t_window = self.binner.bin_size*self.binner.B
        elif t_window != self.t_window:
            self.t_window = t_window
        if t_step is None:
            t_step = self.binner.bin_size
        elif t_step != self.t_step:
            self.t_step   = t_step
            self.connect_to(self.pc)


    def _percent_to_time(self, percent):
        len_frame = len(self.pc.ts)
        totime = int(np.round((percent * len_frame)))
        if totime < 0: 
            totime = 0
        elif totime > len_frame - 1:
            totime = len_frame - 1
        return totime


    def partition(self, training_range=[0.0, 0.5], valid_range=[0.5, 0.6], testing_range=[0.6, 1.0],
                        low_speed_cutoff={'training': True, 'testing': False}, v_cutoff=None):

        self.train_range = training_range
        self.valid_range = valid_range
        self.test_range  = testing_range
        self.low_speed_cutoff = low_speed_cutoff

        if v_cutoff is None:
            self.v_cutoff = self.pc.v_cutoff
        else:
            self.v_cutoff = v_cutoff

        self.train_time = [self.pc.ts[self._percent_to_time(training_range[0])], 
                           self.pc.ts[self._percent_to_time(training_range[1])]]
        self.valid_time = [self.pc.ts[self._percent_to_time(valid_range[0])], 
                           self.pc.ts[self._percent_to_time(valid_range[1])]]
        self.test_time  = [self.pc.ts[self._percent_to_time(testing_range[0])], 
                           self.pc.ts[self._percent_to_time(testing_range[1])]]

        self.train_idx = np.arange(self._percent_to_time(training_range[0]),
                                   self._percent_to_time(training_range[1]))
        self.valid_idx = np.arange(self._percent_to_time(valid_range[0]),
                                   self._percent_to_time(valid_range[1]))
        self.test_idx  = np.arange(self._percent_to_time(testing_range[0]),
                                   self._percent_to_time(testing_range[1]))

        if low_speed_cutoff['training'] is True:
            self.train_idx = self.train_idx[self.pc.v_smoothed[self.train_idx]>self.v_cutoff]
            self.valid_idx = self.valid_idx[self.pc.v_smoothed[self.valid_idx]>self.v_cutoff]

        if low_speed_cutoff['testing'] is True:
            self.test_idx = self.test_idx[self.pc.v_smoothed[self.test_idx]>self.v_cutoff]

        if self.verbose:
            print('{0} training samples\n{1} validation samples\n{2} testing samples'.format(self.train_idx.shape[0],
                                                                                             self.valid_idx.shape[0],
                                                                                             self.test_idx.shape[0]))

    def save(self, filename):
        torch.save(self, filename)

    def get_data(self, minimum_spikes=2, first_unit_is_noise=True):
        '''
        Connect to pc first and then set the partition parameter. After these two we can get data
        The data strucutre is different for RNN and non-RNN decoder
        Therefore each decoder subclass has its own get_partitioned_data method
        In low_speed periods, data should be removed from train and valid:
        '''
        assert(self.pc.ts.shape[0] == self.pc.pos.shape[0])

        X = self.pc.get_scv(self.t_window) # t_step is None unless specified, using pc.ts
        y = self.pc.pos[1:] # the initial position is not predictable
        assert(X.shape[0]==y.shape[0])

        self.train_X, self.train_y = X[self.train_idx], y[self.train_idx]
        self.valid_X, self.valid_y = X[self.valid_idx], y[self.valid_idx]
        self.test_X,  self.test_y  = X[self.test_idx], y[self.test_idx]

        if minimum_spikes>0:
            self.train_X, self.train_y = mua_count_cut_off(self.train_X, self.train_y, minimum_spikes)
            self.valid_X, self.valid_y = mua_count_cut_off(self.valid_X, self.valid_y, minimum_spikes)
            self.test_X,  self.test_y  = mua_count_cut_off(self.test_X,  self.test_y,  minimum_spikes)

        if first_unit_is_noise:
            self.train_X = self.train_X[:,1:]
            self.valid_X = self.valid_X[:,1:]
            self.test_X  = self.test_X[:,1:]

        return (self.train_X, self.train_y), (self.valid_X, self.valid_y), (self.test_X, self.test_y) 


    def r2_score(self, y_predict, y_true, multioutput=True):
        if multioutput is True:
            score = r2_score(y_true, y_predict, multioutput='raw_values')
        else:
            score = r2_score(y_true, y_predict)
        if self.verbose:
            print('r2 score: {}\n'.format(score))
        return score

    def auto_pipeline(self, smooth_sec=2, remove_first_neuron=False):
        '''
        example for evaluate the funciton of acc[partition]:
        >>> dec = NaiveBayes(t_window=500e-3, t_step=60e-3)
        >>> dec.connect_to(pc)
        >>> r_scores = []
        >>> partition_range = np.arange(0.1, 1, 0.05)
        >>> for i in partition_range:
        >>>     dec.partition(training_range=[0, i], valid_range=[0.5, 0.6], testing_range=[i, 1],
        >>>                   low_speed_cutoff={'training': True, 'testing': True})
        >>>     r_scores.append(dec.auto_pipeline(2))
        '''
        (X_train, y_train), (X_valid, y_valid), (self.X_test, self.y_test) = self.get_data(minimum_spikes=2, 
                                                                                           first_unit_is_noise=remove_first_neuron)
        self.fit(X_train, y_train, remove_first_neuron=remove_first_neuron)
        self.predicted_y = self.predict(self.X_test)
        self.smooth_factor  = int(smooth_sec/self.pc.t_step) # 2 second by default
        self.sm_predicted_y = smooth(self.predicted_y, self.smooth_factor)
        score = self.r2_score(self.y_test, self.sm_predicted_y) # ! r2 score is not symmetric, needs to be (true, prediction)
        return score

    def score(self, smooth_sec=2, remove_first_neuron=False):
        '''
        dec.score will first automatically train the decoder (fit) and then test it (predict). 
        The training set and test set are also automatically saved in dec.X_train and dec.X_test
        The training and test label are saved in dec.y_train and dec.y_test
        '''
        return self.auto_pipeline(smooth_sec=smooth_sec, remove_first_neuron=remove_first_neuron)

    def plot_decoding_err(self, dec_pos, real_pos, err_percentile = 90, N=None, err_max=None):
        err = abs(dec_pos - real_pos)
        # err[:,0] /= self.pc.maze_length[0]
        # err[:,1] /= self.pc.maze_length[1]
        dt = self.t_step
        if N is None:
            N = err.shape[0]
        return plot_err_2d(dec_pos, real_pos, err, dt, N, err_percentile, err_max)



class NaiveBayes(Decoder):
    """
    NaiveBayes Decoder for position prediction (input X, output y) 
    where X is the spike bin matrix (B_bins, N_neurons)
    where y is the 2D position (x,y)

    Examples:
    -------------------------------------------------------------
    from spiketag.analysis import NaiveBayes, smooth

    dec = NaiveBayes(t_window=250e-3, t_step=50e-3)
    dec.connect_to(pc)

    dec.partition(training_range=[0.0, .7], valid_range=[0.5, 0.6], testing_range=[0.6, 1.0], 
                  low_speed_cutoff={'training': True, 'testing': True})
    (train_X, train_y), (valid_X, valid_y), (test_X, test_y) = dec.get_data(minimum_spikes=0)
    dec.fit(train_X, train_y)

    predicted_y = dec.predict(test_X)
    dec_pos  = smooth(predicted_y, 60)
    real_pos = test_y
    
    score = dec.evaluate(dec_pos, real_pos)

    # optional (cost time):
    # dec.plot_decoding_err(dec_pos, real_pos);

    To get scv matrix to hack (check the size):
    -------------------------------------------------------------
    _scv = dec.pc.scv
    _scv = dec.pc.scv[dec.train_idx]
    _scv = dec.pc.scv[dec.test_idx]
    _y = dec.predict(_scv)
    post2d = dec.post_2d

    Test real-time prediction:
    -------------------------------------------------------------
    _y = dec.predict_rt(_scv[8])

    """
    def __init__(self, t_window, t_step=None):
        super(NaiveBayes, self).__init__(t_window, t_step)
        self.name = 'NaiveBayes'
        self.rt_post_2d, self.binned_pos = None, None  # these two variables can be used for real-time visualization in the playground
        self._disable_neuron_idx = None  # mask out neuron
        
    def fit(self, X=None, y=None, remove_first_neuron=False):
        '''
        Naive Bayes place decoder fitting use precise spike timing to compute the representation 
        (Rather than using binned spike count vector in t_window)
        Therefore the X and y is None for the consistency of the decoder API
        '''
        if remove_first_neuron: # remove the first neuron (the one classified as noise)
            self.pc.spk_time_dict = {i: self.pc.spk_time_dict[i+1] for i in range(len(self.pc.spk_time_dict.keys())-1)}
        self.pc.get_fields(self.pc.spk_time_dict, self.train_time[0], self.train_time[1], v_cutoff=self.v_cutoff, rank=False)
        # self.pc.get_fields()
        self.fields = self.pc.fields
        self.spatial_bin_size, self.spatial_origin = self.pc.bin_size, self.pc.maze_original

        # for real-time decoding on incoming bin from BMI   
        self.poisson_matrix = self.t_window*self.fields.sum(axis=0)
        self.log_fr = np.log(self.fields) # make sure Fr[Fr==0] = 1e-12

    def predict(self, X):
        X_arr = X.copy()

        if len(X_arr.shape) == 1:
            X_arr = X_arr.reshape(1,-1)

        if self._disable_neuron_idx is not None:
            self.neuron_idx = [_ for _ in range(self.fields.shape[0]) if _ not in self._disable_neuron_idx]
            firing_bins = X_arr[:, self.neuron_idx]
            place_fields = self.fields[self.neuron_idx]
        else:
            firing_bins = X_arr
            place_fields = self.fields

        self.post_2d = bayesian_decoding(place_fields, firing_bins, t_window=self.t_window)
        binned_pos = argmax_2d_tensor(self.post_2d)
        y = binned_pos*self.spatial_bin_size + self.spatial_origin
        return y

    def predict_rt(self, X):
        if X.shape[0]>1:
            X = np.sum(X, axis=0)  # X is (B_bins, N_neurons) spike count matrix, we need to sum up B bins to decode the full window
        else:
            X = X.ravel()

        if self._disable_neuron_idx is not None:
            self.neuron_idx = [_ for _ in range(self.fields.shape[0]) if _ not in self._disable_neuron_idx]
            firing_bins = X[:, self.neuron_idx]
            place_fields = self.fields[self.neuron_idx]
        else:
            firing_bins = X
            place_fields = self.fields

        suv_weighted_log_fr = licomb_Matrix(firing_bins, np.log(place_fields))
        self.rt_post_2d = np.exp(suv_weighted_log_fr - self.t_window*place_fields.sum(axis=0))
        self.rt_post_2d /= self.rt_post_2d.sum()
        self.rt_pred_binned_pos = argmax_2d_tensor(self.rt_post_2d)
        y = self.rt_pred_binned_pos*self.spatial_bin_size + self.spatial_origin
        return y, self.rt_post_2d

    def drop_neuron(self, _disable_neuron_idx):
        if type(_disable_neuron_idx) == int:
            _disable_neuron_idx = [_disable_neuron_idx]
        self._disable_neuron_idx = _disable_neuron_idx
