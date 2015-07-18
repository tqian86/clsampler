#-*- coding: utf-8 -*-

from __future__ import print_function, division
import numpy as np
import pandas as pd
import sys, copy, random, mimetypes, os.path, gzip
from time import time

def lognormalize(x, temp = 1):
    """Normalize a vector of logprobabilities to probabilities that sum up to 1.
    Optionally accepts an annealing temperature that does simple annealing.
    """
    if type(x) is list: x = np.array(x)

    x = x - np.max(x)
    # anneal
    xp = np.power(np.exp(x), temp)
    return xp / xp.sum()

def sample(a, p):
    """Step sample from a discrete distribution using CDF
    """
    if (len(a) != len(p)):
        raise Exception('a != p')
    p = np.array(p)
    p = p / p.sum()
    r = random.random()
    n = len(a)
    total = 0           # range: [0,1]
    for i in xrange(n):
        total += p[i]
        if total > r:
            return a[i]
    return a[i]

class BaseSampler(object):

    def __init__(self, cl_mode=False, cl_device=None, sample_size=1000, cutoff=None,
                 output_to_stdout=False, record_best=False, annealing = False, debug_mumble = False):
        """Initialize the class.
        """
        if cl_mode:
            import pyopencl as cl
            import pyopencl.array, pyopencl.tools, pyopencl.clrandom
            if cl_device == 'gpu':
                gpu_devices = []
                for platform in cl.get_platforms():
                    try: gpu_devices += platform.get_devices(device_type=cl.device_type.GPU)
                    except: pass
                self.ctx = cl.Context(gpu_devices)
            elif cl_device == 'cpu':
                cpu_devices = []
                for platform in cl.get_platforms():
                    try: cpu_devices += platform.get_devices(device_type=cl.device_type.CPU)
                    except: pass
                self.ctx = cl.Context([cpu_devices[0]])
            else:
                self.ctx = cl.create_some_context()

            self.queue = cl.CommandQueue(self.ctx)
            self.mf = cl.mem_flags
            self.device = self.ctx.get_info(cl.context_info.DEVICES)[0]
            self.device_type = self.device.type
            self.device_compute_units = self.device.max_compute_units

        self.cl_mode = cl_mode
        self.data = []
        self.N = 0 # number of data points

        # sampling parameters
        self.sample_size = sample_size
        self.iteration = 0
        self.thining = 1
        self.burnin = 0
        self.gpu_time = 0
        self.total_time = 0

        # stochastic search parameters
        self.best_sample = (None, None) # (sample, loglikelihood)
        self.record_best = record_best
        self.best_diff = []
        self.no_improv = 0
       
        # annealing parameters, if used
        self.annealing = annealing
        self.annealing_temp = 1
        
        self.debug_mumble = debug_mumble
        
    def read_csv(self, filepath, header = True):
        """Read data from a csv file.
        """
        # determine if the type file is gzip
        filetype, encoding = mimetypes.guess_type(filepath)
        if encoding == 'gzip':
            self.data = pd.read_csv(filepath, compression=True)
        else:
            self.data = pd.read_csv(filepath)

        self.N = self.data.shape[0]

        # set up references to the file paths
        self.source_filepath = filepath
        self.source_dirname = os.path.dirname(filepath) + '/'
        self.source_filename = os.path.basename(filepath).split('.')[0]

        self.output_fp = gzip.open(self.source_filepath + '-{0}-samples.csv.gz'.format(type(self).__name__), 'w')
        return True

    def direct_read_data(self, data):
        self.data = data

    def set_temperature(self, iteration):
        """Set the temperature of simulated annealing as a function of sampling progress.
        """
        if self.annealing is False:
            self.annealing_temp = 1.0
            return

        if iteration < self.sample_size * 0.2:
            self.annealing_temp = 0.2
        elif iteration < self.sample_size * 0.3:
            self.annealing_temp = 0.4
        elif iteration < self.sample_size * 0.4:
            self.annealing_temp = 0.6
        elif iteration < self.sample_size * 0.5:
            self.annealing_temp = 0.8
        else:
            self.annealing_temp = 1.0

    def do_inference(self, output_file = None):
        """Perform inference. This method does nothing in the base class.
        """
        return

    def auto_save_sample(self, sample):
        """Save the given sample as the best sample if it yields
        a larger log-likelihood of data than the current best.
        """
        new_logprob = self._logprob(sample)
        # if there's no best sample recorded yet
        if self.best_sample[0] is None and self.best_sample[1] is None:
            self.best_sample = (sample, new_logprob)
            if self.debug_mumble: print('Initial sample generated, loglik: {0}'.format(new_logprob), file=sys.stderr)
            return

        # if there's a best sample
        if new_logprob > self.best_sample[1]:
            self.no_improv = 0
            self.best_diff.append(new_logprob - self.best_sample[1])
            self.best_sample = (copy.deepcopy(sample), new_logprob)
            if self.debug_mumble: print('New best sample found, loglik: {0}'.format(new_logprob), file=sys.stderr)
            return True
        else:
            self.no_improv += 1
            return False

    def no_improvement(self, threshold=100):
        if len(self.best_diff) == 0: return False
        if self.no_improv > threshold or np.mean(self.best_diff[-threshold:]) < .1:
            print('Too little improvement in loglikelihood for %s iterations - Abort searching' % threshold, file=sys.stderr)
            return True
        return False

    def _logprob(self, sample):
        """Compute the logliklihood of data given a sample. This method
        does nothing in the base class.
        """
        return 0