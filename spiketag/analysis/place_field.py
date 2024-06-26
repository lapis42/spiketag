import numpy as np
import pandas as pd
from scipy import signal
import matplotlib.pyplot as plt
import matplotlib as mpl
import seaborn as sns
from matplotlib.pyplot import cm
from scipy.interpolate import interp1d
from sklearn.preprocessing import label_binarize
from sklearn.metrics import r2_score
import torch
import torch.nn.functional as F
from torch.utils.data import Dataset
import torchvision
from ipywidgets import interact
from .core import pos2speed, argmax_2d_tensor, spk_time_to_scv, firing_pos_from_scv, smooth, get_corr_field
from .core import sliding_window_to_feature
from .manifold import FA
from ..base import SPKTAG
from ..utils import colorbar
from ..utils.plotting import colorline
from ..realtime import bmi_packet


def info_bits(Fr, P):
    Fr[Fr==0] = 1e-25
    MFr = sum(P.ravel()*Fr.ravel())
    return sum(P.ravel()*(Fr.ravel()/MFr)*np.log2(Fr.ravel()/MFr))


def info_sparcity(Fr, P):
    Fr[Fr==0] = 1e-25
    MFr = sum(P.ravel()*Fr.ravel())
    return sum(P.ravel()*Fr.ravel()**2/MFr**2)

def plot_goal(ax, goal_center, goal_radius=15, color='k', fill=False, alpha=1, linewidth=3):
    goal_region = plt.Circle(goal_center, goal_radius, color=color, fill=fill, alpha=alpha, linewidth=linewidth)
    ax.add_patch(goal_region)

def plot_2d_trajectory(ax, ts, pos, speed, goal_center, goal_radius=15, markersize=15):
    '''
    plot 2D trajectory of the animal, along with possible goal region
    if speed is not None, use speed to color the trajectory, otherwise use time stamps to color the trajectory
    if goal_center is not None, plot the goal region

    Parameters
    ----------
    ax : matplotlib axis
    ts : time stamps: (N,)
    pos : 2D position of the animal: (N, 2)
    speed : speed of the animal: (N,)
    goal_center : center of the goal region: (2,)
    goal_radius : radius of the goal region: (float)
    markersize : size of the marker indicating the animal position
    '''
    if speed is None:
        ax.scatter(pos[:, 0], pos[:, 1], c=ts, s=markersize, cmap=plt.cm.jet)
    else:
        ax.scatter(pos[:, 0], pos[:, 1], c=speed, s=markersize, cmap=plt.cm.jet)
    if goal_center is not None:
        plot_goal(ax, goal_center=goal_center, goal_radius=goal_radius)

def _get_toi(cue_ts, pos_ts, speed_ts):
    '''
    find the time that cue transition while the animal is within 15 cm of the cue
    return a vector of time of interest (toi)

    Parameters
    ----------
    cue_ts : TimeSeries that stores the cue position with timestamps
    pos_ts : TimeSeries that stores the animal position with timestamps
    speed_ts : TimeSeries that stores the animal speed with timestamps

    Returns
    -------
    toi : a vector of time of interest (toi) that is used to find the trial start and end time
    '''
    cue_to_cue_dist = cue_ts.diff().norm()
    pos_to_cue_dist = (cue_ts - pos_ts).norm()[:-1]
    trial_end_idx = np.where((cue_to_cue_dist.data>15) & (pos_to_cue_dist.data<=15))[0]
    trial_end_t = pos_ts.t[trial_end_idx]
    toi = np.append(0, trial_end_t)
    return toi


def _get_trial_time(pos_ts, cue_ts, speed_ts, speed_threshold_to_start_trial=1, goal_radius=15):
    '''
    first get toi (time of interest), which is the time when the cue transition while the animal is within 15 cm of the cue, which is very close to the true trial end time. 
    We need to further process toi to get the true trial start and end time. To understand why:
    In preBMI, cue transition can happen after the animal has already entered the cue zone, we need to find the time when the animal is within 15 cm of the cue zone.
    In preBMI, animal could wait long before starting another trial, we need to find the time when the animal is moving faster than 4 cm/s, and use that as the trial start time.

    Therefre, use following method:
        1 - get toi, which is the time when the cue transition while the animal is within 15 cm of the cue
        2 - iterate through toi and find the time when the animal is moving faster than 4 cm/s, defined as trial start
        3 - iterate through last three seconds of toi and find the time when the animal just touches the cue, defined as trial end

    Parameters
    ----------
    pos_ts : TimeSeries that stores the animal position with timestamps
    cue_ts : TimeSeries that stores the cue position with timestamps
    speed_ts : TimeSeries that stores the animal speed with timestamps

    Returns
    -------
    trial_time : a 2D array of trial start and end time, (N, 2), N is the number of trials, each row is a trial.
    '''
    # step 1: get toi
    toi = _get_toi(cue_ts, pos_ts, speed_ts)

    trial_start_t = []
    trial_end_t   = []
    for i in range(1, len(toi)):
        # step 2: find the time when the animal is moving faster than 4 cm/s, defined as trial start
        try:
            _speed_ts = speed_ts.between(toi[i-1] + 2, toi[i]) # give animal 5 seconds to have reward
            _trial_start = _speed_ts.t[np.argmax(_speed_ts.data.ravel() > speed_threshold_to_start_trial)]

            # step 3: find the time when the animal just touches the cue, defined as trial end
            t_last_3_seconds_before_trial_end = np.arange(toi[i]-2, toi[i], 0.1)
            new_trial_end = t_last_3_seconds_before_trial_end[np.argmax((pos_ts.searchsorted(t_last_3_seconds_before_trial_end) -
                                                                         cue_ts.searchsorted(t_last_3_seconds_before_trial_end)).norm().data < goal_radius)] 
            if new_trial_end - _trial_start > 2:
                trial_start_t.append(_trial_start)
                trial_end_t.append(new_trial_end)
        except:
            pass

    trial_start_t = np.array(trial_start_t)
    trial_end_t = np.array(trial_end_t)

    trial_time = np.vstack((trial_start_t, trial_end_t)).T
    return trial_time



class place_field(Dataset):
    '''
    place cells class contains `ts` `pos` `scv` for analysis
    load_log for behavior
    load_spktag for spike data
    get_fields for computing the representaions using spike and behavior data
    '''
    def __init__(self, pos, v_cutoff=5, bin_size=2.5, kernlen=9, kernstd=2, ts=None, t_step=None, maze_range=None):
        '''
        resample the trajectory with new time interval
        reinitiallize with a new t_step (dt)
        '''
        if ts is None:
            ts = np.arange(0, pos.shape[0]*t_step, t_step)
            self.t_step = t_step
        self.ts, self.pos = ts, pos
        self._ts_init, self._pos_init = ts, pos
        self._ts_restore, self._pos_restore = ts, pos
        self.spk_time_array, self.spk_time_dict = None, None
        self.df = {}

        # key parameters for initialization (before self.initialize we need to align behavior with ephys) 
        self.bin_size = bin_size
        self.v_cutoff = v_cutoff
        self.kernlen = kernlen
        self.kernstd = kernstd
        self.maze_range = maze_range

        # decide output variables (i.e., returned data when calling pc[:N], pc[N:], pc[:])
        self.output_variables = ['scv', 'pos']

        # calculate the binned maze, cutoff based on the speed, and the occupation map
        self.initialize()

    def __call__(self, t_step):
        '''
        resample the trajectory with new time interval
        reinitiallize with a new t_step (dt)
        '''
        fs = self.fs 
        new_fs = 1/t_step
        self.t_step = t_step
        self.ts, self.pos = self.interp_pos(self.ts, self.pos, self.t_step)
        self.initialize()

    def restore(self):
        self.ts, self.pos = self._ts_restore, self._pos_restore

    def __len__(self):
        return len(self.ts[1:])

    def __getitem__(self, idx):
        '''
        return variables in the pc.output_variables for analysis or training ML models
        t_window = pc.t_step
        scv = pc.get_scv(t_window)
        pc.output_variables = ['scv', 'pos']
        train_size = 0.5
        N = int(len(pc)*train_size)
        train_X, train_y = pc[:N]
        test_X, test_y = pc[N:]
        '''
        output = [getattr(self, var)[idx] for var in self.output_variables]
        # use :k to make sure outputs has the same size as pos can contain one more item than scv or speed
        k = np.array([len(_) for _ in output]).min() 
        return [x[:k] for x in output]

    @property
    def output_variables(self):
        '''
        This variable controls what variables would be returned when calling pc[:] or pc[i:j]
        '''
        return self._output_variables

    @output_variables.setter
    def output_variables(self, var=['scv', 'pos']):
        '''
        examples: 
        ['scv', 'pos']
        ['scv', 'speed']
        ['scv', 'cue_pos']
        ['scv', 'binned_pos']
        ['scv', 'label_pos']
        ['scv', 'pos', 'cue_pos', 'speed']
        '''
        self._output_variables = var

    @property
    def dt(self):
        return self.ts[1] - self.ts[0]

    @property
    def t_total(self):
        return self.ts[-1] - self.ts[0]

    @property
    def fs(self):
        self._fs = 1/(self.ts[1]-self.ts[0])
        return self._fs

    def interp_pos(self, t, pos, new_dt):
        '''
        convert irregularly sampled pos into regularly sampled pos
        N is the dilution sampling factor. N=2 means half of the resampled pos
        example:
        >>> new_fs = 200.
        >>> pc.ts, pc.pos = pc.interp_pos(ts, pos, N=fs/new_fs)
        '''
        dt = t[1] - t[0]
        x, y = interp1d(t, pos[:,0], fill_value="extrapolate"), interp1d(t, pos[:,1], fill_value="extrapolate")
        new_t = np.arange(t[0], t[-1], new_dt)
        new_pos = np.hstack((x(new_t).reshape(-1,1), y(new_t).reshape(-1,1)))
        return new_t, new_pos 


    def align_with_recording(self, recording_start_time, recording_end_time, replay_offset=0):
        '''
        replay_offset should be 0 if and only if pc.ts[0] is the actual ephys start time 0
        the ephys actual start time 0 = recording_start_time + replay_offset

        ts before alignment   |--------------------|
        behavior  start:      |
        behavior    end:                           |
        recording start:         |------------
        recording   end:          ------------|
        replay_offset  :             |
        ts after alignment           |------------| 
        '''
        self.ts += replay_offset   # 0 if the ephys is not offset by replaying through neural signal generator
        self.pos = self.pos[np.logical_and(self.ts>=recording_start_time, self.ts<=recording_end_time)]
        self.ts  =  self.ts[np.logical_and(self.ts>=recording_start_time, self.ts<=recording_end_time)]
        self.t_start = self.ts[0]
        self.t_end   = self.ts[-1]
        self._ts_restore, self._pos_restore = self.ts, self.pos


    def initialize(self):
        self.get_maze_range()
        self.get_speed() 
        self.get_occupation_map()
        self.pos_df = pd.DataFrame(np.hstack((self.ts.reshape(-1,1), self.pos)),
                                   columns=['time', 'x', 'y'])
        # self.binned_pos = (self.pos-self.maze_original)//self.bin_size


    def get_maze_range(self):
        if self.maze_range is None:
            self.maze_range = np.vstack((self.pos.min(axis=0), self.pos.max(axis=0))).T
            self._maze_original = self.maze_range[:,0] # the left, down corner location
        else:
            self.maze_range = np.array(self.maze_range)
            self._maze_original = self.maze_range[:,0] # the left, down corner location

    @property
    def maze_center(self):
        self._maze_center = self.maze_original[0]+self.maze_length[0]/2, self.maze_original[1]+self.maze_length[1]/2
        return self._maze_center

    @property
    def maze_original(self):
        return self._maze_original

    @property
    def maze_length(self):
        return np.diff(self.maze_range, axis=1).ravel()

    @property
    def maze_ratio(self):
        return self.maze_length[0]/self.maze_length[1]

    @property
    def binned_pos(self):
        binned_pos = (self.pos-self.maze_original)//self.bin_size
        return binned_pos
    
    @property
    def onehot_pos(self):
        return self.binned_pos_2_onehot(self.binned_pos, xbins=self.O.shape[1], ybins=self.O.shape[0])
    
    @property
    def label_pos(self):
        return self.binned_pos_2_label(self.binned_pos, xbins=self.O.shape[1])

    @property
    def prob_pos(self):
        xbins = self.O.shape[1]
        ybins = self.O.shape[0]
        Y = self.onehot_pos.reshape(-1, 1, ybins, xbins) # batch, channel, H, W
        kernel_size = 3
        T = F.conv2d(input=torch.from_numpy(Y).float(),
                     weight=torch.from_numpy(self.gkern(kernel_size,1)).reshape(1,1,kernel_size,kernel_size).float(), 
                     padding=kernel_size//2).squeeze()
        T = T/T.reshape(-1, xbins*ybins).sum(axis=-1).reshape(-1,1,1)
        return T

    def binned_pos_2_real_pos(self, binned_pos):
        pos = binned_pos*self.bin_size + self.maze_original
        return pos
    
    def binned_pos_2_label(self, binned_pos, xbins=None):
        if xbins is None:
            xbins = self.O.shape[1]
        y = binned_pos[:,0]+binned_pos[:,1]*xbins
        return y

    def binned_pos_2_onehot(self, binned_pos, xbins=None, ybins=None):
        if xbins is None:
            xbins = self.O.shape[1]
        if ybins is None:
            ybins = self.O.shape[0]
        label_y = self.binned_pos_2_label(binned_pos, xbins)
        Y = label_binarize(label_y, classes=range(xbins*ybins))
        return Y
    
    def label_pos_2_binned_pos(self, label_y, xbins=None):
        if xbins is None:
            xbins = self.O.shape[1]
        binned_pos = np.vstack((label_y%xbins, label_y//xbins)).T
        return binned_pos

    def real_pos_2_binned_pos(self, real_pos, interger_output=True):
        if real_pos.ndim == 1:
            real_pos = real_pos.reshape(1,-1)
        if interger_output:
            binned_pos = (real_pos - self.maze_original)//self.bin_size
        else:
            binned_pos = (real_pos - self.maze_original)/self.bin_size
        return binned_pos
    
    def real_pos_2_soft_pos(self, pos, kernel_size=7):
        binned_y = self.real_pos_2_binned_pos(pos)
        Y = self.binned_pos_2_onehot(binned_y).reshape(-1, 1, self.O.shape[0], self.O.shape[1])
        T = F.conv2d(input=torch.from_numpy(Y).float(),
                    weight=torch.from_numpy(self.gkern(kernel_size,1)).reshape(1,1,kernel_size,kernel_size).float(), 
                    padding=kernel_size//2).squeeze()
        T = T/T.reshape(-1, self.O.shape[0]*self.O.shape[1]).sum(axis=-1).reshape(-1,1,1)
        return T.squeeze().numpy()
    
    def pos_2_speed(self, pos, ts=None):
        return pos2speed(pos, ts)

    def get_speed(self):
        '''
        self.ts, self.pos is required

        To consider that some/many place cells start firing before moving, and stop firing a few seconds after moving, we 
        need a wider smoothing window. 

        v_smoothed_wide is a larger window to calculate the low_speed_idx

        The `low_speed_idx` are thos index of `ts` and `pos` that are too slow to be considered to calculate the place field. 
        '''
        # self.v = np.linalg.norm(np.diff(self.pos, axis=0), axis=1)/np.diff(self.ts)
        # self.v = np.hstack((self.v[0], self.v))
        self.v = self.pos_2_speed(self.pos, self.ts)
        self.v = np.linalg.norm(self.v, axis=1)
        self.v_smoothed = smooth(self.v.reshape(-1,1), int(np.round(self.fs))).ravel()
        self.v_smoothed_wide = 2 * smooth(self.v.reshape(-1,1), 6*int(np.round(self.fs))).ravel()
        self.low_speed_idx = np.where(self.v_smoothed_wide < self.v_cutoff)[0]

        self.df['pos'] = pd.DataFrame(data=np.hstack((self.pos, self.v_smoothed.reshape(-1,1))), index=self.ts, 
                                        columns=['x','y','v'])
        self.df['pos'].index.name = 'ts'

        '''
        # check speed:
        f, ax = plt.subplots(1,1, figsize=(18,8))
        offset=20000
        plot(ts[offset:1000+offset], v[offset:1000+offset])
        plot(ts[offset:1000+offset], v_smoothed[offset:1000+offset])
        ax.axhline(5, c='m', ls='-.')
        '''
        # return v_smoothed, v

    def plot_speed(self, start=None, stop=None, v_cutoff=5):
        if start is None:
            start = self.ts[0]
        if stop is None:
            stop = self.ts[-1]
        fig, ax = plt.subplots(1,1, figsize=(18,5))
        period = np.logical_and(self.ts>start, self.ts<stop)
        plt.plot(self.ts[period], self.v[period], alpha=.7)
        plt.plot(self.ts[period], self.v_smoothed[period], lw=2)
        plt.plot(self.ts[period], self.v_smoothed_wide[period], lw=5, alpha=0.5)
        ax.axhline(v_cutoff, c='m', ls='-.')
        sns.despine()
        return fig
        
    def get_occupation_map(self, start=None, end=None):
        '''
        f, ax = plt.subplots(1,2,figsize=(20,9))
        ax[0].plot(self.pos[:,0], self.pos[:,1])
        ax[0].plot(self.pos[0,0], self.pos[0,1], 'ro')
        ax[0].plot(self.pos[-1,0], self.pos[-1,1], 'ko')
        ax[0].pcolormesh(self.X, self.Y, self.O, cmap=cm.hot_r)
        sns.heatmap(self.O[::-1]*self.dt, annot=False, cbar=False, ax=ax[1])
        '''
        # if maze_range != 'auto':
        #     self.maze_range = maze_range
        self.maze_size = np.array([self.maze_range[0][1]-self.maze_range[0][0], self.maze_range[1][1]-self.maze_range[1][0]])
        self.nbins = self.maze_size/self.bin_size
        self.nbins = self.nbins.astype(int)

        if start is None and end is None:
            start, end = self.ts[0], self.ts[-1]
        self.high_speed_idx = np.where((self.v_smoothed_wide >= self.v_cutoff) & (self.ts>=start) & (self.ts<=end))[0]
        occupation, self.x_edges, self.y_edges = np.histogram2d(x=self.pos[self.high_speed_idx,0], 
                                                                y=self.pos[self.high_speed_idx,1], 
                                                                bins=self.nbins, range=self.maze_range)
        self.X, self.Y = np.meshgrid(self.x_edges, self.y_edges)
        self.O = occupation.T.astype(int)  # Let each row list bins with common y range.
        self.O_smoothed = signal.convolve2d(self.O, self.gkern(2, 1), boundary='symm', mode='same')
        self.P = self.O/float(self.O.sum()) # occupation prabability


    def plot_occupation_map(self, cmap=cm.viridis):
        f, ax = plt.subplots(1,2,figsize=(20,9))
        ax[0].plot(self.pos[:,0], self.pos[:,1])
        ax[0].plot(self.pos[0,0], self.pos[0,1], 'ro')
        ax[0].plot(self.pos[-1,0], self.pos[-1,1], 'go')
        ax[0].pcolormesh(self.X, self.Y, self.O, cmap=cmap)
        ax[1].pcolormesh(self.X, self.Y, self.O, cmap=cmap)
        plt.show()

    @property
    def map_binned_size(self):
        return np.array(np.diff(self.maze_range)/self.bin_size, dtype=np.int).ravel()[::-1]

    @staticmethod
    def gkern(kernlen=21, std=2):
        """Returns a 2D Gaussian kernel array."""
        gkern1d = signal.gaussian(kernlen, std=std).reshape(kernlen, 1)
        gkern2d = np.outer(gkern1d, gkern1d)
        gkern2d /= gkern2d.sum()
        return gkern2d

    def _get_firing_pos(self, spk_times):
        spk_ts_idx = np.searchsorted(self.ts, spk_times) - 1
        spk_ts_idx = spk_ts_idx[spk_ts_idx>0]
        # idx = np.array([_ for _ in spk_ts_idx if _ not in self.low_speed_idx], dtype=np.int)
        idx = spk_ts_idx[~np.in1d(spk_ts_idx, self.low_speed_idx)]
        # idx = np.setdiff1d(spk_ts_idx, self.low_speed_idx)
        firing_ts  = self.ts[idx]
        firing_pos = self.pos[idx]
        return firing_pos

    def _get_field(self, spk_times):
        '''
        spk_times: spike times in seconds (an Numpy array), for example:
        array([   1.38388,    1.6384 ,    1.7168 , ..., 2393.72648, 2398.52484, 2398.538  ])

        Can be read out of spk_time_dict, which is loaded from pc.load_spktag() or pc.load_spkdf() methods.
        spk_times = pc.spk_time_dict[2]

        Used by `get_fields` method to calculate the place fields for all neurons in pc.spk_time_dict.
        '''
        self.firing_pos = self._get_firing_pos(spk_times)     
        self.firing_map, x_edges, y_edges = np.histogram2d(x=self.firing_pos[:,0], y=self.firing_pos[:,1], 
                                                           bins=self.nbins, range=self.maze_range)
        self.firing_map = self.firing_map.T
        np.seterr(divide='ignore', invalid='ignore')
        self.FR = self.firing_map / (self.O * self.dt)
        # if spk_times.shape[0]>0: # for cross-validation, it is possible that some cell never fire in the training set, then its FR should be all zero
        #     mean_firing_rate = spk_times.shape[0]/time_span
        #     self.FR /= mean_firing_rate
        self.FR[np.isnan(self.FR)] = 0
        self.FR[np.isinf(self.FR)] = 0
        self.FR_smoothed = signal.convolve2d(self.FR, self.gkern(self.kernlen, self.kernstd), boundary='symm', mode='same')
        return self.FR_smoothed

    def scv_2_fc(self, scv, pos):
        '''
        convert spike count vector (scv) and positions (pos) from time series to feature count and class count
        via one-hot position encoding and matrix multiplication (contraction in time)

        intput:
            scv is (T, N) spike count vector in time (N is number of neurons/features, T is number of samples)
            pos is (T, 2) positons in time 

        output:
            fc: {} that contains:
            feature_count: feature (spike) counts in binned states (S, N) (S is number of states)
            class_count:   class   (state) counts in binned states (S,) 
            classes:       unique classes
            feature_count_non_zero_: feature (spike) counts in binned states (K, N) (K is number of nonzero states)
            class_count_non_zero:    classes (state) counts in binned states (K,) 
            classes_non_zero_: unique classes that were visited at least once

        internal:
            onehot_pos: (T, S) (S)tate in time, each state is a one-hot binned positon vector 
            feature_count_ = onehot_pos.T@scv : (S,T)@(T,N) = (S,N), is state-feature matrix, since each state is one-hot
                                                                     it becomes a feature counts matrix
            non_zero_feature_count_: (K, N), exclude those state that were never reached in all time
        
        ! Note: `feature_count_non_zero_` == `feature_count_` in `sklearn.naive_bayes.MultinomialNB`
        !       `class_count_non_zero_`   == `class_count_` in `sklearn.naive_bayes.MultinomialNB`
        !       `classes_non_zero_`       == `classes_` in `sklearn.naive_bayes.MultinomialNB`
        '''
        T, N = scv.shape[0], scv.shape[1]
        assert(T==pos.shape[0])
        scv = torch.from_numpy(scv).float()
        binned_pos = self.real_pos_2_binned_pos(pos)
        onehot_pos = torch.from_numpy(self.binned_pos_2_onehot(binned_pos)).float()
        # feature_count_ is firing count at each one-hot location
        self.feature_count_ = onehot_pos.T@scv # ! counts feature in scv at each one-hot location by contracting time dim
        self.class_count_ = onehot_pos.sum(axis=0) # ! counts state (class) at each one-hot location by contracting time dim
        self.classes_ = np.arange(self.class_count_.shape[0]) # ! all possible states label (not one-hot encoding)
        self.feature_count_non_zero_ = self.feature_count_[self.feature_count_.sum(axis=1) != 0]
        self.class_count_non_zero_ = self.class_count_[self.class_count_ != 0]
        self.classes_non_zero_ = self.class_count_.nonzero().ravel().numpy()
        fc = {}
        fc['feature_count'] = self.feature_count_
        fc['class_count'] = self.class_count_
        fc['classes'] = self.classes_
        fc['feature_count_non_zero_'] = self.feature_count_non_zero_
        fc['class_count_non_zero_'] = self.class_count_non_zero_
        fc['classes_non_zero_'] = self.classes_non_zero_
        return fc

    def fc_2_firing_maps(self, feature_count, classes):
        '''
        convert `feature_count` and `classes` (in `sklearn.naive_bayes.MultinomialNB`) to 
         - place fields (fields):  fields = pc.fc_2_firing_maps(mnb.feature_count_, mnb.classes_) 
         - occumation field (O):   O = pc.fc_2_firing_maps(mnb.class_count_, mnb.classes_)
        # 0. get training and testing data
        pc(0.05)  
        t_window = 0.5
        X = pc.get_scv(t_window)
        y = pc.label_pos[1:]
        speed_threshold = 8
        total_idx = np.where(pc.v_smoothed > speed_threshold)[0][:-1]
        train_idx, test_idx = train_test_split(total_idx, test_size=0.5, shuffle=False)
        train_X, train_y = X[train_idx], y[train_idx]
        test_X = X[test_idx]
        test_y = pc.binned_pos_2_real_pos(pc.label_pos_2_binned_pos(y[test_idx]))

        # 1. fit and check 
        mnb = MNB(alpha=1)
        mnb.fit(train_X, train_y);
        _dec_y = mnb.predict(test_X)
        _dec_y = pc.binned_pos_2_real_pos(pc.label_pos_2_binned_pos(_dec_y))
        dec_y = smooth(_dec_y, 50)
        r2_score(test_y, dec_y, multioutput='raw_values')
        dec.plot_decoding_err(test_y, dec_y);

        # 2. get feature_count and classes
        fields = pc.fc_2_firing_maps(mnb.feature_count_, mnb.classes_)
        O = pc.fc_2_firing_maps(mnb.class_count_, mnb.classes_)
        np.seterr(divide='ignore', invalid='ignore')
        fields = fields/t_window/O
        fields[np.isinf(fields)] = 0
        fields[np.isnan(fields)] = 0

        # 3. comparing fields produced by different method
        pc.get_fields() # update pc.fields by turning spike times into firing map, then place fields 
        @interact(i=(0, pc.n_fields-1, 1))
        def compare_fields(i=0):
            fig, ax = plt.subplots(1, 3, figsize=(4*5+1,5))
            c1 = ax[0].imshow(fields[i], cmap=cm.hot, origin='lower');
            plt.colorbar(mappable=c1, ax=ax[0]);    
            field = signal.convolve2d(fields[i], pc.gkern(pc.kernlen, pc.kernstd), boundary='symm', mode='same')
            c1 = ax[1].imshow(field, cmap=cm.hot, origin='lower');
            plt.colorbar(mappable=c1, ax=ax[1]);
            c2 = ax[2].imshow(pc.fields[i], cmap=cm.hot, origin='lower')
            plt.colorbar(mappable=c2, ax=ax[2]);
            plt.show()
        '''
        if feature_count.ndim == 1:
            feature_count = feature_count.reshape(-1, 1)
        S, N = feature_count.shape  # (S)tate, (N)eurons
        pos_idx = self.label_pos_2_binned_pos(classes).astype(np.int)
        fields = np.zeros((N, self.O.shape[0], self.O.shape[1])).astype(np.float)
        for i in range(N):
            fields[i, pos_idx[:, 1], pos_idx[:, 0]] = feature_count[:, i]
        return fields

    def firing_maps_2_fields(self, firing_maps, occupation_map, t_window=None, smooth=True):
        '''
        convert firing count maps (firing_maps) into 
                firing rate maps  (fields = firing_maps/occupation_map/t_window)
                firing rate maps are place fields
        '''
        if t_window is None:
            t_window = self.t_step
        fields = np.zeros_like(firing_maps)
        np.seterr(divide='ignore', invalid='ignore')
        fields = firing_maps/occupation_map/t_window
        fields[np.isinf(fields)] = 0
        fields[np.isnan(fields)] = 0

        if smooth is True:
            for i, field in enumerate(fields):
                field = signal.convolve2d(field, self.gkern(
                    self.kernlen, self.kernstd), boundary='symm', mode='same')
                fields[i] = field
        return fields

    def get_firing_maps_from_scv(self, scv, pos):
        fc = self.scv_2_fc(scv, pos)
        self.firing_maps = self.fc_2_firing_maps(fc['feature_count_non_zero_'], fc['classes_non_zero_'])

        # or alternatively: (but fc_2_firing_maps is more general solution for `feature_count_`` that cannot be directly reshaped)
        # firing_maps = self.feature_count_.reshape(self.O.shape[0], self.O.shape[1], -1)
        # firing_maps = firing_maps.permute((2,0,1)).numpy()
        # self.firing_maps = firing_maps
        return self.firing_maps

    def get_fields_from_scv(self, scv, pos, t_window=None):
        '''
        intput:
            scv is (T, N) spike count vector in time 
            pos is (T, 2) positons in time 
            t_window: time window of each row in scv (default: pc.t_step for non-overlapping scv)
        output:
            fields: place fields (spatial rate map) in binned space (N, ybins, xbins) 
        example:
            # get fields directly from scv and pos (non-overlapping window by default)
            t_window = pc.t_step
            pos = pc.pos[1:]
            scv = pc.get_scv(t_window)
            fields = pc.get_fields_from_scv(scv, pos, t_window)

            # comparing fields produced by different method
            pc.get_fields() # update pc.fields by turning spike times into firing map, then place fields 
            @interact(i=(0, pc.n_fields-1, 1))
            def compare_fields(i=0):
                fig, ax = plt.subplots(1,2, figsize=(13,5))
                c1 = ax[0].imshow(fields[i], cmap=cm.hot, origin='lower');
                plt.colorbar(mappable=c1, ax=ax[0]);
                c2 = ax[1].imshow(pc.fields[i], cmap=cm.hot, origin='lower')
                plt.colorbar(mappable=c2, ax=ax[1]);
                plt.show()
        
        Note:
            unlike pc.get_fields() method, here user is responsible to exclude 
            those `scv` and `pos` when animal at low speed. 
            This method is more flexible because user can decide which scv and pos they want to use to build place fields
        '''
        # self.firing_maps = self.get_firing_maps_from_scv(scv, pos)
        fc = self.scv_2_fc(scv, pos)
        self.firing_maps = self.fc_2_firing_maps(fc['feature_count_non_zero_'], fc['classes_non_zero_'])
        self.occupation_map = self.fc_2_firing_maps(fc['class_count_non_zero_'], fc['classes_non_zero_'])
        self.fields = self.firing_maps_2_fields(self.firing_maps, self.occupation_map, t_window)
        return self.fields

    def get_field(self, spk_time_dict, neuron_id, start=None, end=None):
        '''
        wrapper of _get_field method
        calculate the place field of a single neuron (neuron_id) in a dictionary of spike times (spk_time_dict)

        Also, can restrict the time range of the place field by `start` and `end`.
        '''
        spk_times = spk_time_dict[neuron_id]
        ### for cross-validation and field stability check
        ### calculate representation from `start` to `end`
        if start is not None and end is not None:
            spk_times = spk_times[np.logical_and(start<=spk_times, spk_times < end)]
        self._get_field(spk_times)


    def _plot_field(self, trajectory=False, cmap='viridis', marker=True, alpha=0.5, markersize=5, markercolor='m'):
        f, ax = plt.subplots(1,1,figsize=(13,10));
        pcm = ax.pcolormesh(self.X, self.Y, self.FR_smoothed, cmap=cmap);
        plt.colorbar(pcm, ax=ax, label='Hz');
        if trajectory:
            ax.plot(self.pos[:,0], self.pos[:,1], alpha=0.8);
            ax.plot(self.pos[0,0], self.pos[0,1], 'ro');
            ax.plot(self.pos[-1,0],self.pos[-1,1], 'ko');
        if marker:
            ax.plot(self.firing_pos[:,0], self.firing_pos[:,1], 'o', 
                    c=markercolor, alpha=alpha, markersize=markersize);
        return f,ax

    def get_fields(self, spk_time_dict=None, start=None, end=None, v_cutoff=None, rank=True, N_fields=None):
        '''
        spk_time_dict is dictionary start from 0: (each spike train is a numpy array) 
        {0: spike trains for neuron 0
         1: spike trains for neuron 1 
         2: spike trains for neuron 2
         ...
         N: spike trains for neuron N}
        '''
        self.get_occupation_map(start=start, end=end) # ! critical for updating the self.O to correctly calculate the place field

        if spk_time_dict is None:
            spk_time_dict = self.spk_time_dict

        if N_fields is None:
            self.n_fields = len(spk_time_dict.keys())
        else:
            self.n_fields = N_fields
            
        self.n_units  = self.n_fields
        self.fields = np.zeros((self.n_fields, self.O.shape[0], self.O.shape[1]))
        self.fields_sharp = np.zeros((self.n_fields, self.O.shape[0], self.O.shape[1]))
        self.firing_pos_dict = {}

        if v_cutoff is None:
            self.get_speed()    # ! critical for generating `low_speed_idx`
        else:
            self.v_cutoff = v_cutoff
            self.get_speed()    # ! critical for generating `low_speed_idx`

        for i in range(self.n_fields):
            if i in spk_time_dict.keys():
                ### get place fields from neuron i
                self.get_field(spk_time_dict, i, start, end)
                self.fields_sharp[i] = self.FR
                self.fields[i] = self.FR_smoothed
                self.firing_pos_dict[i] = self.firing_pos
            else:
                self.firing_pos_dict[i] = np.array([]) # make sure self.firing_pos_dict[i].shape[0] == 0

        self.fields[self.fields==0] = 1e-25

        if rank is True:
            self.rank_fields(metric_name='spatial_bit_smoothed_spike')

    def get_corr_field(self, pv):
        '''
        Calculate the correlation field of a population vector and a rate vector for each position in a maze.
        Args:
            pv (np.ndarray): The population vector of shape (N,). e.g., pc.scv[20]

        Internal variables in or to the function get_corr_field:
            fields (np.ndarray): The place fields of shape (N, M, M), where M is the size of the maze. pc.fields in spiketag can
                                be used here. e.g., pc.fields
            The rate vector (rv = pc.fields[:, x, y]) is a 1-dimensional array of shape (N,) that represents the firing rate of each
            neuron at a specific position in the maze. 

        Returns:
        np.ndarray: The correlation field of shape (M, M) containing the correlation coefficients between pv and rv for each
                     position in the maze.

        Example:
        i = 20 # the frame number
        cf = pc.get_corr_field(pc.scv[i])
        plt.imshow(cf, vmin=0, vmax=1)
        plt.plot(binned_pos[i, 0], binned_pos[i, 1], 'C3o', ms=20);
        '''
        return get_corr_field(pv, self.fields)

    @property
    def max_firing_rate(self):
        return np.array([self.fields[i].max() for i in range(self.fields.shape[0])])

    def plot_fields(self, idx=None, nspks=None, N=10, size=3, cmap='hot', marker=False, markersize=1, alpha=0.8, order=False, min_peak_rate=None):
        '''
        order: if True will plot with ranked fields according to the metric 
        '''
        if min_peak_rate is not None and idx is None:
            idx = np.where(self.metric['peak_rate'] > min_peak_rate)[0] # peak rate larger than threshold
        if idx is None: # plot all fields
            nrow = self.n_fields/N + 1
            ncol = N
            fig = plt.figure(figsize=(ncol*size, nrow*size));
            # plt.tight_layout();
            plt.subplots_adjust(wspace=None, hspace=None);
            for i in range(self.n_fields):
                ax = fig.add_subplot(nrow, ncol, i+1);
                if order:
                    field_id = self.sorted_fields_id[i]
                else:
                    field_id = i
                pcm = ax.pcolormesh(self.X, self.Y, self.fields[field_id], cmap=cmap);
                ax.set_title('#{0}: {1:.2f}Hz'.format(field_id, self.fields[field_id].max()), fontsize=20)
                ax.set_xticks([])
                ax.set_yticks([])
                ax.set_aspect(self.maze_ratio)
                if marker:
                    ax.plot(self.firing_pos_dict[field_id][:,0], self.firing_pos_dict[field_id][:,1], 
                                                              'mo', markersize=markersize, alpha=alpha)
            plt.grid(False)
            plt.show();

        else:
            nrow = len(idx)/N + 1
            ncol = N
            fig = plt.figure(figsize=(ncol*size, nrow*size));
            plt.subplots_adjust(wspace=None, hspace=None);
            for i, field_id in enumerate(idx):
                ax = fig.add_subplot(nrow, ncol, i+1);
                pcm = ax.pcolormesh(self.X, self.Y, self.fields[field_id], cmap=cmap);
                ax.set_title('#{0}: {1:.2f}Hz'.format(field_id, self.fields[field_id].max()))
                ax.set_xticks([])
                ax.set_yticks([])
                if nspks is not None:
                    ax.set_xlabel('{} spikes'.format(nspks[i]))
                if marker:
                    ax.plot(self.firing_pos_dict[field_id][:,0], self.firing_pos_dict[field_id][:,1], 
                                                              'mo', markersize=markersize, alpha=alpha)
                ax.set_aspect(self.maze_ratio)
            plt.grid(False)
            plt.show();

        return fig


    def plot_field(self, i=0, cmap=None, alpha=.3, markersize=10, markercolor='#66f456', trajectory=True):
        '''
        plot ith place field with information in detail, only called after `pc.get_fields(pc.spk_time_dict, rank=True)`
        example:

        @interact(i=(0, pc.n_units-1, 1))
        def view_fields(i=0):
            pc.plot_field(i)

        '''
        if cmap is None:
            cmap = sns.cubehelix_palette(as_cmap=True, dark=0.05, light=1.2, reverse=True);
        neuron_id = self.sorted_fields_id[i]
        self._get_field(self.spk_time_dict[neuron_id])
        f,ax = self._plot_field(cmap=cmap, alpha=alpha, markersize=markersize, 
                         markercolor=markercolor, trajectory=trajectory);
        n_bits = self.metric['spatial_bit_spike'][neuron_id]
        p_rate = self.metric['peak_rate'][neuron_id]
        ax.set_title('neuron {0}: max firing rate {1:.2f}Hz, {2:.3f} bits'.format(neuron_id, p_rate, n_bits))
        return f,ax

    def plot_fields_image(self, fields, cmap='hot', show=True, invert_y=True):
        fields = torch.from_numpy(fields).unsqueeze(1) # N, C, H, W
        if invert_y:
            fields = torch.flip(fields, dims=(2,)) # invert the H axis (since our data is inverted in y-axis)
        fields_img = torchvision.utils.make_grid(fields, padding=3, pad_value=1, nrow=10,
                                               normalize=True,
                                               scale_each=True).mean(axis=0)
        fig, ax = plt.subplots(1,1, figsize=(8,8))
        ax.imshow(fields_img.numpy(), cmap=cmap);
        plt.axis(False);
        return fig
        
    def rank_fields(self, metric_name):
        '''
        metric_name: spatial_bit_spike, spatial_bit_smoothed_spike, spatial_sparcity
        '''
        self.metric = {}
        self.metric['peak_rate'] = np.zeros((self.n_fields,))
        self.metric['avg_rate']  = np.zeros((self.n_fields,))
        self.metric['spatial_bit_spike'] = np.zeros((self.n_fields,))
        self.metric['spatial_bit_smoothed_spike'] = np.zeros((self.n_fields,))
        self.metric['spatial_sparcity'] = np.zeros((self.n_fields,))

        for neuron_id in range(self.fields.shape[0]):
            self.metric['peak_rate'][neuron_id] = self.fields[neuron_id].max()
            self.metric['avg_rate'][neuron_id]  = self.firing_pos_dict[neuron_id].shape[0]/self.t_total
            self.metric['spatial_bit_spike'][neuron_id] = info_bits(self.fields[neuron_id], self.P) 
            self.metric['spatial_bit_smoothed_spike'][neuron_id] = info_bits(self.fields[neuron_id], self.P)
            self.metric['spatial_sparcity'][neuron_id] = info_sparcity(self.fields[neuron_id], self.P)

        self.sorted_fields_id = np.argsort(self.metric[metric_name])[::-1]


    def raster(self, ls, colorful=False, xlim=None, ylim=None):
        color_list = ['C{}'.format(i) for i in range(self.n_units)]
        fig, ax = plt.subplots(1,1, figsize=(15,10));
        if colorful:
            ax.eventplot(positions=self.spk_time_array, colors=color_list, ls=ls, alpha=.2);
        else:
            ax.eventplot(positions=self.spk_time_array, colors='k', ls=ls, alpha=.2);
        if xlim is not None:
            ax.set_xlim(xlim);
        if ylim is not None:
            ax.set_ylim(ylim);
        ax.set_ylabel('unit')
        ax.set_xlabel('time (secs)')
        sns.despine()
        return fig


    def load_spkdf(self, df_file, fs=25000., start=None, end=None, replay_offset=0, show=False, N_fields=None):
        '''
        core function: load spike dataframe in spktag folder (to get Spikes)
        This function also align ephys with behavior and compute the place fields of each found units in the `df_file`

        Example:
        ------------
        pc = place_field(pos=pos, ts=ts)
        pc.load_spkdf(spktag_file_df)
        pc.report()
        '''
        print('--------------- place cell object: load spktag dataframe ---------------\r\n')
        self.spike_df = pd.read_pickle(df_file)
        self.spike_df['frame_id'] /= fs
        self.spike_df.set_index('spike_id', inplace=True)
        self.spike_df.index = self.spike_df.index.astype(int)
        self.spike_df.index -= self.spike_df.index.min()
        self.spike_df.index.name = 'spike_id'
        self.spike_df = self.spike_df[self.spike_df.frame_id > 0]
        self.df['spk'] = self.spike_df
        self.spk_time_dict = {i: self.spike_df.frame_id.to_numpy()[self.spike_df.index.to_numpy() == i]
                              for i in self.spike_df.index.unique().sort_values().to_numpy()}
        self.df['spk'].reset_index(inplace=True)
        self.n_units = np.sort(self.spike_df.spike_id.unique()).shape[0]
        self.n_groups = np.sort(self.spike_df.group_id.unique()).shape[0]
        print('1. Load the spktag dataframe\r\n    {} units are found in {} electrode-groups\r\n'.format(self.n_units, self.n_groups))

        if start is None:
            start = self.spike_df.frame_id.iloc[0]
        if end is None:
            end = self.spike_df.frame_id.iloc[-1]
        self.align_with_recording(start, end, replay_offset)
        
        # after align_with_recording we have the correct self.ts and self.pos
        self.total_spike = len(self.spike_df)
        self.total_time = self.ts[-1] - self.ts[0]
        self.mean_mua_firing_rate = self.total_spike/self.total_time

        print('2. Align the behavior and ephys data with {0} offset\r\n    starting at {1:.3f} secs, end at {2:.3f} secs, step at {3:.3f} ms\r\n    all units mount up to {4:.3f} spikes/sec\r\n'.format(replay_offset, start, end, self.dt*1e3, self.mean_mua_firing_rate))

        print('3. Calculate the place field during [{},{}] secs\r\n    spatially bin the maze, calculate speed and occupation_map with {}cm bin_size\r\n    dump spikes when speed is lower than {}cm/secs\r\n'.format(start, end, self.bin_size, self.v_cutoff))                      
        self.initialize()
        self.get_fields(self.spk_time_dict, start=start, end=end, rank=True, N_fields=N_fields)

        try:
            self.df['spk']['x'] = np.interp(self.df['spk']['frame_id'], self.ts, self.pos[:,0])
            self.df['spk']['y'] = np.interp(self.df['spk']['frame_id'], self.ts, self.pos[:,1])
            self.df['spk']['v'] = np.interp(self.df['spk']['frame_id'], self.ts, self.v_smoothed)
            print('4. Interpolate the position and speed to each spikes, check `pc.spike_df`\r\n')
        except:
            print('! Fail to fill the position and speed to the spike dataframe')
        if show is True:
            self.field_fig = self.plot_fields();     
        print('------------------------------------------------------------------------')   
    
    def bmi_packet(self, t0=0, t1=None):
        '''
        get all bmi packets (from t0 to t1) for simulating the bmi recording
        Example:
            # ! 1. prepare bmi packets and a binner
            bmi_data = pc.bmi_packet(0, 25) # get bmi packet from 0 to 25 seconds
            binner = Binner(bin_size=0.1, n_id=scv_full.shape[1], n_bin=7)
            # ! 2. prepare the receving/decoding code
            rt_scv = []
            @binner.connect
            def on_decode(X):
                rt_scv.append(X)
            # ! 3. simulate the recording
            for bmi_output in bmi_data:
                binner.input(bmi_output)
        '''
        if t1 is not None:
            spk_df = self.spike_df[(self.spike_df.frame_id<=t1) & (self.spike_df.frame_id>t0)]
        else:
            spk_df = self.spike_df[(self.spike_df.frame_id>t0)]
        _bmi_packet = bmi_packet(spk_df)
        return _bmi_packet

    def report(self, cmap='hot', order=False, min_peak_rate=1):
        print('occupation map from {0:.2f} to {1:.2f}, with speed cutoff:{2:.2f}'.format(self.ts[0], self.ts[-1], self.v_cutoff))
        self.plot_occupation_map();
        self.plot_speed(self.ts[0], self.ts[-1]//10, v_cutoff=self.v_cutoff);
        self.plot_fields(N=10, cmap=cmap, order=order, min_peak_rate=min_peak_rate);


    def load_spktag(self, spktag_file, show=False):
        '''
        1. load spktag
        2. extract unit time stamps
        3. calculate the place fields
        4. rank based on its information bit
        5. (optional) plot place fields of each unit
        check pc.n_units, pc.n_fields and pc.metric after this
        '''
        spktag = SPKTAG()
        spktag.load(spktag_file)
        self.spktag_file = spktag_file
        self.spk_time_array, self.spk_time_dict = spktag.spk_time_array, spktag.spk_time_dict
        self.get_fields(self.spk_time_dict, rank=True)
        if show is True:
            self.field_fig = self.plot_fields();


    def get_scv(self, t_window, B_bins=None, FA_dim=0):
        '''
        The offline binner to calculate the spike count vector (scv)
        run `pc.load_spktag(spktag_file)` first
        Input:
            - t_window is the window to count spikes
            t_step defines the sliding window size
            
            - B_bins: binning parameter used in bmi application,
            if B_bins is not None, then for each frame, the _scv:(B_Bins, N_neurons) 
            
            - FA_dim: factor analysis to remove the independent noise from scv
            if FA_dim is 0, then FA will be bypassed, no reconstruction will be done
        '''
        self.scv = spk_time_to_scv(self.spk_time_dict, t_window=t_window, ts=self.ts)
        if FA_dim>0:
            self.fa = FA(n_components=FA_dim)
            self.fa.fit(self.scv) #scv (spike count vector): (n_samples, n_units)
            self.reconstructed_scv = self.fa.reconstruct(self.scv) 
            self.resampled_scv = self.fa.sample_manifold(self.scv) 
            self.scv = self.reconstructed_scv
        self.mua_count = self.scv.sum(axis=1)
        if B_bins is not None:
            self.scv = sliding_window_to_feature(self.scv, B_bins-1)
            self.scv = self.scv.reshape(self.scv.shape[0], B_bins, self.n_units)
        return self.scv
    
    def get_data(self, t_window=None, B_bins=None, FA_dim=0):
        '''
        get data as same format in real-time BMI application
        Input:
            - t_window: integration window, default to t_step if non-overlapping binning is used
            - B_bins: bins used for each frame
            
        Output:
            - scv: spike count vector (N_frames, B_bins, N_neurons)
            - pos: position output (N_frames, 2); each row(frame) is a position: (x,y)
            - hdv: head direction speed (N_frames, 2); each row(frame) is a movement: (dx, dy)
        '''
        if t_window is None:
            t_window = self.t_step
        scv = self.get_scv(t_window, B_bins, FA_dim)
        pos = self.pos
        hdv = smooth(self.pos_2_speed(pos), int(2/self.t_step))
        pos = self.pos[B_bins:]
        hdv = hdv[B_bins:]
        return scv, pos, hdv

    # def plot_epoch(self, time_range, figsize=(5,5), marker=['ro', 'wo'], markersize=15, alpha=.5, cmap=None, legend_loc=None):
    #     '''
    #     plot trajactory within time_range: [[a0,b0],[a1,b1]...]
    #     with color code indicate the speed.  
    #     '''
        
    #     gs = dict(height_ratios=[20,1])
    #     fig, ax = plt.subplots(2,1,figsize=(5, 5), gridspec_kw=gs)

    #     for i, _time_range in enumerate(time_range):  # ith epoches
    #         epoch = np.where((self.ts<_time_range[1]) & (self.ts>=_time_range[0]))[0]
            
    #         if cmap is None:
    #             cmap = mpl.cm.cool
    #         norm = mpl.colors.Normalize(vmin=self.v_smoothed.min(), vmax=self.v_smoothed.max())

    #         ax[0] = colorline(x=self.pos[epoch, 0], y=self.pos[epoch, 1], 
    #                           z=self.v_smoothed[epoch]/self.v_smoothed.max(), #[0,1] 
    #                           cmap=cmap, ax=ax[0])
    #         if i ==0:
    #             ax[0].plot(self.pos[epoch[-1], 0], self.pos[epoch[-1], 1], marker[0], markersize=markersize, alpha=alpha, label='end')
    #             ax[0].plot(self.pos[epoch[0], 0], self.pos[epoch[0], 1], marker[1], markersize=markersize, alpha=alpha, label='start')
    #         else:
    #             ax[0].plot(self.pos[epoch[-1], 0], self.pos[epoch[-1], 1], marker[0], markersize=markersize, alpha=alpha)
    #             ax[0].plot(self.pos[epoch[0], 0], self.pos[epoch[0], 1], marker[1], markersize=markersize, alpha=alpha)                

    #     ax[0].set_xlim(self.maze_range[0]);
    #     ax[0].set_ylim(self.maze_range[1]);

    #     # ax[0].set_title('trajectory in [{0:.2f},{1:.2f}] secs'.format(_time_range[0], _time_range[1]))
    #     if legend_loc is not None:
    #         ax[0].legend(loc=legend_loc)
        
    #     cb = mpl.colorbar.ColorbarBase(ax[1], cmap=cmap,
    #                                     norm=norm,
    #                                     orientation='horizontal')
    #     cb.set_label('speed (cm/sec)')
    #     return ax

    def get_trial_time(self, speed_threshold_to_start_trial=5, goal_radius=15):
        '''
        we define a trial as a period of time with trial_start_t and trial_end_t,
        trial_start_t is when the animal is moving at a speed above a threshold
        trial_end_t is when the animal touch the goal cue

        we use these variables to find out when each trial starts and ends:
            pc.ts.shape, pc.pos.shape, pc.cue_ts.shape, pc.cue_pos.shape, pc.v_smoothed.shape
        
        Parameters
        ----------
        speed_threshold_to_start_trial : int, optional
            DESCRIPTION. The default is 5.
        goal_radius : int, optional
            DESCRIPTION. The default is 15.

        Returns
        -------
        trial_time : (N_trials, 2) array, each row is a trial time: [trial_start_t, trial_end_t]
        '''
        from spiketag.analysis import TimeSeries as TS
        pos_ts = TS(self.ts, self.pos)
        speed_ts = TS(self.ts, self.v_smoothed)
        cue_ts = TS(self.cue_ts, self.cue_pos[:, :2]).searchsorted(pos_ts.t)
        # print(cue_ts.shape, pos_ts.shape, speed_ts.shape)
        trial_time = _get_trial_time(pos_ts, cue_ts, speed_ts, speed_threshold_to_start_trial, goal_radius)

        trial_duration = np.diff(trial_time, axis=1).ravel()
        trial_duration = TS(None, trial_duration)
        trial_duration_ci_lower, trial_duration_ci_upper = trial_duration.ci()
        trial_duration_mean, trial_duration_std = trial_duration.data.mean(), trial_duration.data.std()
        print('trial duration: mean={0:.2f}+/-{1:.2f} secs, ci=[{2:.2f},{3:.2f}]'.format(trial_duration_mean, 
                                                                                         trial_duration_std, 
                                                                                         trial_duration_ci_lower[0], 
                                                                                         trial_duration_ci_upper[0]))

        return trial_time

    def plot_duration(self, t0, t1, goal_radius=15, markersize=15, color_as_speed=True, ax=None, verbose=True):
        '''
        plot the trajectory between t0 and t1, with color code indicate the speed or time (depending on color_as_speed)
        if there exists self.cue_ts and self.cue_pos, plot the goal position

        Parameters
        ----------
        t0 : float, start time
        t1 : float, end time
        goal_radius : int, optional
            DESCRIPTION. The default is 15.
        markersize : int, optional
            DESCRIPTION. The default is 15.
        color_as_speed : bool, optional
            DESCRIPTION. The default is True.
        ax : matplotlib.axes._subplots.AxesSubplot, optional
            DESCRIPTION. The default is None.
        verbose : bool, optional, if true, print the trial start/end, current distance, current speed. 
            DESCRIPTION. The default is True.
        '''
        from spiketag.analysis import TimeSeries as TS

        if ax is None:
            fig, ax = plt.subplots(1, 1, figsize=(5, 5))

        pos_ts = TS(self.ts, self.pos)
        speed_ts = TS(self.ts, self.v_smoothed)
        _pos_ts = pos_ts.between(t0, t1)
        _speed_ts = speed_ts.between(t0, t1)
        
        # check if there exists self.cue_ts and self.cue_pos, if not, plot only the animal trajectory, if yes, plot the goal position
        if not hasattr(self, 'cue_ts') or not hasattr(self, 'cue_pos'):
            goal_pos = None
            if color_as_speed:
                plot_2d_trajectory(ax, _pos_ts.t, _pos_ts.data, _speed_ts.data, goal_pos, goal_radius=goal_radius, markersize=markersize)
            else:
                plot_2d_trajectory(ax, _pos_ts.t, _pos_ts.data, None,           goal_pos, goal_radius=goal_radius, markersize=markersize)
        else:
            cue_ts = TS(self.cue_ts, self.cue_pos[:, :2]).searchsorted(pos_ts.t) # goal position is stored in self.cue_pos[:, :2]
            _cue_ts = cue_ts.between(t0, t1)
            goal_pos = _cue_ts.data[-1]
            if color_as_speed:
                plot_2d_trajectory(ax, _pos_ts.t, _pos_ts.data, _speed_ts.data, goal_pos, goal_radius=goal_radius, markersize=markersize)
            else:
                plot_2d_trajectory(ax, _pos_ts.t, _pos_ts.data, None,           goal_pos, goal_radius=goal_radius, markersize=markersize)

            # if there is a goal, we also show the distance between the animal and the goal and the speed of the animal entering the goal region
            # print(goal_pos.shape, _pos_ts.data.shape)
            if verbose:
                current_distance = np.sqrt(np.sum((_pos_ts.data[-1] - goal_pos)**2))
                current_speed = _speed_ts.data[-1][0]
                ax.set_title(f'{t0:.1f} - {t1:.1f} s, distance: {current_distance:.1f} cm, last speed: {current_speed:.1f} cm/s', fontsize=12)

        ax.set_xlim(self.maze_range[0]);
        ax.set_ylim(self.maze_range[1]);
        ax.set_aspect('equal')
        return ax

    def plot_all_trials(self, N=6, goal_radius=15, markersize=15, color_as_speed=True, speed_threshold_to_start_trial=5):
        '''
        Usage
        -----
        fig = pc.plot_all_trials(N=6, goal_radius=15, markersize=20, color_as_speed=False)

        Function
        --------
        plot all trials, each trial is a subplot, each row has N subplots
        call self.plot_duration() to plot each trial, see self.plot_duration() for more details

        Parameters
        ----------
        N : int, optional
            number of subplots in each row. The default is 6.
        goal_radius : int, optional
            DESCRIPTION. The default is 15.
        markersize : int, optional
            DESCRIPTION. The default is 15.
        color_as_speed : bool, optional
            DESCRIPTION. The default is True.

        Returns
        -------
        Fig
        '''
        trial_time = self.get_trial_time(speed_threshold_to_start_trial=speed_threshold_to_start_trial)
        n_trials = len(trial_time)
        n_rows, n_cols = n_trials//N+1, N
        fig, ax = plt.subplots(n_rows, n_cols, figsize=(n_cols*2, n_rows*2))
        for k in range(n_trials):
            t0, t1 = trial_time[k]
            _ax = ax[k//n_cols, k % n_cols]
            self.plot_duration(t0, t1, goal_radius=goal_radius, markersize=markersize,
                               color_as_speed=color_as_speed, ax=_ax, verbose=False)
            # set axis markers invisible
            _ax.axes.yaxis.set_visible(False)
            _ax.axes.xaxis.set_visible(False)
            _ax.set_title(f'trial{k+1}: {t1-t0:.1f} s', fontsize=12)
            # minimize the margin between subplots
            plt.subplots_adjust(wspace=0.2, hspace=0.2)
        return fig

    def to_file(self, filename):
        df_all_in_one = pd.concat([self.pos_df, self.spike_df], sort=True)
        df_all_in_one.to_pickle(filename+'.pd')

    def to_dec(self, t_step=0.1, t_window=0.8, t_smooth=3, type='bayesian',  
                     first_unit_is_noise=True, min_speed=4, FA_dim=0, 
                     min_bit=0.1, min_peak_rate=1.5, min_avg_rate=0.5, firing_rate_modulation=True, 
                     neuron_idx=None, LSTM=True,
                     verbose=True, **kwargs):
        '''
        kwargs example:
        - training_range: [0, 0.5]
        - valid_range: [0.5, 0.7]
        - testing_range: [0.7, 1.0]
        - low_speed_cutoff: {'training': True, 'testing': True}
        - max_noise: for data augmentation
        - 
        '''
        # first visualize the training data and test data (sanity check)
        training_range = kwargs['training_range'] if 'training_range' in kwargs.keys() else [0.0, 1.0]
        valid_range    = kwargs['valid_range'] if 'valid_range'    in kwargs.keys() else [0.0, 1.0]
        testing_range  = kwargs['testing_range'] if 'testing_range'  in kwargs.keys() else [0.0, 1.0]
        low_speed_cutoff = kwargs['low_speed_cutoff'] if 'low_speed_cutoff' in kwargs.keys() else {'training': True, 'testing': True}

        if neuron_idx is None:
            self.neuron_idx = ((self.metric['spatial_bit_spike']>min_bit) & 
                               (self.metric['peak_rate']>min_peak_rate) & 
                               (self.metric['avg_rate']>min_avg_rate)).nonzero()[0]
        else:
            self.neuron_idx = neuron_idx
        self.drop_neuron_idx = np.delete(np.arange(self.n_units), self.neuron_idx)
        if first_unit_is_noise and 0 not in self.drop_neuron_idx:
            self.drop_neuron_idx = np.append(0, self.drop_neuron_idx)
        print(f'{self.neuron_idx.shape[0]} out of {self.n_units} neurons are selected, {self.drop_neuron_idx.shape[0]} neurons are dropped')
        
        N = self.pos.shape[0]
        fig, ax = plt.subplots(1,2,figsize=(11,5))
        ax[0].plot(self.pos[int(N*training_range[0]):int(N*training_range[1]),0], 
                   -self.pos[int(N*training_range[0]):int(N*training_range[1]),1]);
        ax[0].set_title(f'training_range: {training_range[0]}-{training_range[1]}')
        ax[1].plot(self.pos[int(N*testing_range[0]):int(N*testing_range[1]), 0], 
                   -self.pos[int(N*testing_range[0]):int(N*testing_range[1]), 1])
        ax[1].set_title(f'testing_range: {testing_range[0]}-{testing_range[1]}')
        plt.show();
        
        if type == 'bayesian':
            from spiketag.analysis import NaiveBayes
            dec = NaiveBayes(t_step=t_step, t_window=t_window)
            dec.connect_to(self)
            dec.resample(t_step=t_step, t_window=t_window)
            dec.partition(training_range=training_range, 
                          valid_range=valid_range, 
                          testing_range=testing_range,
                          low_speed_cutoff=low_speed_cutoff)
            dec.verbose = verbose
            dec.drop_neuron(self.drop_neuron_idx)   # drop the neuron with id 0 which is noise with those fire at super low frequency
            self.smooth_factor = int(t_smooth/t_step)
            dec.smooth_factor = int(t_smooth/t_step)
            score = dec.score(t_smooth=t_smooth, firing_rate_modulation=firing_rate_modulation)
            dec._score = score
            return dec, score

        if type == 'NN':            
            # 1. prepare training and test data
            self(t_step)

            # self.get_scv(t_window=t_step);
            # self.output_variables = ['scv', 'pos']
            # scv_full, pos_full = self[:]
            B_bins = int(t_window/t_step)
            self.smooth_factor = int(t_smooth/t_step)
            n = B_bins - 1
            scv_full_for_test, _, _ = self.get_data(t_window=t_step, B_bins=B_bins, FA_dim=0)
            scv_full, pos_full, hdv_full = self.get_data(t_window=t_step, B_bins=B_bins, FA_dim=FA_dim)
            pos_full = smooth(pos_full, self.smooth_factor) * 1.00
            v = np.linalg.norm(self.pos_2_speed(pos_full)/t_step, axis=1)
            scv_full = scv_full[v>min_speed]
            scv_full_for_test = scv_full_for_test[v>min_speed]
            pos_full = pos_full[v>min_speed]
            scv_full = np.sqrt(scv_full)  # square root spike count for training
            scv_full_for_test = np.sqrt(scv_full_for_test)  # square root spike count for testing
            
            scv = scv_full[:, :, neuron_idx].reshape(scv_full.shape[0], -1) # training data format
            scv_4_test = scv_full_for_test[:, :, neuron_idx].reshape(scv_full_for_test.shape[0], -1) # testing data format
            pos = pos_full
            
            ncells, nsamples = scv.shape[1], scv.shape[0]
            X = scv[int(nsamples*training_range[0]):int(nsamples*training_range[1])]
            y = pos[int(nsamples*training_range[0]):int(nsamples*training_range[1])]
            # X = np.vstack((X[1:], (X[1:] + X[:-1])*0.5))
            # y = np.vstack((y[1:], (y[1:] + y[:-1])*0.5))
            X_test = scv_4_test[int(nsamples*testing_range[0]):int(nsamples*testing_range[1])]
            y_test = pos[int(nsamples*testing_range[0]):int(nsamples*testing_range[1])]
            
            # ! 2. initiate deepnet decoder
            from spiketag.analysis.decoder import DeepOSC
            decoder = DeepOSC(input_dim=ncells, t_step=t_step, t_window=t_window, 
                              hidden_dim=[256, 256], output_dim=2, bn=True, LSTM=LSTM)
            decoder.connect_to(self)
            decoder.neuron_idx = neuron_idx
            decoder.train_X = X
            decoder.train_y = y
            decoder.test_X = X_test
            decoder.test_y = y_test
            print(f'{X.shape[0]} training samples')
            print(f'{X_test.shape[0]} testing samples')
            decoder.smooth_factor = self.smooth_factor

            # 3. training
            decoder.model.bn1.track_running_stats = False
            decoder.model.bn1.running_mean = None
            decoder.model.bn1.running_var = None
            max_noise = kwargs['max_noise'] if 'max_noise' in kwargs.keys() else 1
            max_epoch = kwargs['max_epoch'] if 'max_epoch' in kwargs.keys() else 3000
            lr = kwargs['lr'] if 'lr' in kwargs.keys() else 3e-4
            
            try:
                decoder.fit(X, y, X_test, y_test, max_noise=max_noise, max_epoch=max_epoch, lr=lr, 
                            smooth_factor=self.smooth_factor, cuda=True)
            except KeyboardInterrupt:
                pass
            
            # 4. testing and initialize bn statistics using both the training the testing data
            decoder.model.bn1.track_running_stats = True
            decoder.model.bn1.running_mean = torch.zeros((256,)).float().cuda()
            decoder.model.bn1.running_var = torch.zeros((256,)).float().cuda()
            decoder.predict(X, mode='train', bn_momentum=0.9);
            dec_y = decoder.predict(X_test, mode='train', bn_momentum=0.9)
            dec_y = smooth(dec_y, self.smooth_factor)
            
            # 5. report and save the r2 score
            decoder.plot_decoding_err(y_test, dec_y)
            score = decoder.r2_score(y_test, dec_y)
            decoder._score = score
            
            # 6. To deploy the model - set to cpu mode
            # decoder.predict(X, mode='train', bn_momentum=0.9); # update bn again
            decoder.model.cpu();

            return decoder, score
