#-*- coding: utf-8 -*-

from __future__ import print_function, division
import numpy as np
import pandas as pd
import logging
import sys, copy, random, mimetypes, os.path, gzip
from time import time
from datetime import datetime

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
                 output_to_stdout=False,
                 search=False, search_tolerance = 100, search_data_fit_only = False,
                 annealing = False, debug_mumble = False):
        """Initialize the class.
        """
        if debug_mumble:
            logging.basicConfig(level=logging.INFO)
        
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
        self.cutoff = cutoff
        self.data = []
        self.N = 0 # number of data points

        # sampling parameters
        self.sample_size = sample_size
        self.output_to_stdout = output_to_stdout
        self.iteration = 0
        self.thining = 1
        self.burnin = 0
        self.gpu_time = 0
        self.total_time = 0

        # stochastic search parameters
        self.best_sample = (None, None, None) # (sample, logprobability of model, loglikelihood of data)
        self.search = search
        self.search_data_fit_only = search_data_fit_only
        self.best_diff = []
        self.no_improv = 0
        self.search_tolerance = search_tolerance
       
        # annealing parameters, if used
        self.annealing = annealing
        self.annealing_temp = 1
        
        self.debug_mumble = debug_mumble

    def __param_str__(self):
        return type(self).__name__
        
    def read_csv(self, filepath, obs_vars = ['obs'], header = True):
        """Read data from a csv file.
        """
        # determine if the type file is gzip
        filetype, encoding = mimetypes.guess_type(filepath)
        if encoding == 'gzip':
            self.data = pd.read_csv(filepath, compression='gzip')
        else:
            self.data = pd.read_csv(filepath)

        self.original_data = copy.deepcopy(self.data)
        if self.cutoff:
            self.data = self.data[:self.cutoff]
            
        self.data = self.data[obs_vars]
        self.N = self.data.shape[0]
        return True

    def setup_sample_output(self, filepath):
        # set up references to the file paths
        self.source_filepath = filepath
        self.source_dirname = os.path.dirname(filepath) + '/'
        self.source_filename = os.path.basename(filepath).split('.')[0]

        # set up the name of the output sample file
        self.sample_fn = self.source_dirname + '{0}-{1}-samples-{2}.csv.gz'.format(self.source_filename,
                                                                                   self.__param_str__(),
                                                                                   str(datetime.now()).split('.')[0].replace(' ', '-'))
        
        return True

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

    def better_sample(self, sample):
        """Save the given sample as the best sample if it yields
        a larger log-likelihood of data than the current best.
        """
        new_logprob_model, new_loglik_data = self._logprob(sample)
        # if there's no best sample recorded yet
        if self.best_sample[0] is None:
            self.best_sample = (sample, new_logprob_model, new_loglik_data)
            self.logprob_model, self.loglik_data = new_logprob_model, new_loglik_data
            logging.info('Initial sample generated, logprob of model: {0}, loglik: {1}'.format(new_logprob_model, new_loglik_data))
            return

        # if there's a best sample
        if self.search_data_fit_only:
            better = new_loglik_data - self.best_sample[2]
        else:
            better = new_logprob_model + new_loglik_data - (self.best_sample[1] + self.best_sample[2])
        if better > 0:
            self.no_improv = 0
            self.best_diff.append(better)
            self.logprob_model, self.loglik_data = new_logprob_model, new_loglik_data
            self.best_sample = (copy.deepcopy(sample), new_logprob_model, new_loglik_data)
            logging.info('New best sample found, logprob of model: {0} loglik: {1}'.format(new_logprob_model, new_loglik_data))
            return True
        else:
            self.no_improv += 1
            return False

    def no_improvement(self):
        if len(self.best_diff) == 0: return False
        if self.no_improv > self.search_tolerance:
            logging.warning('Too little improvement in loglikelihood for %s iterations - Abort searching' % self.search_tolerance)
            return True
        return False

    def _logprob(self, sample):
        """Compute the log probability of the model parameters and the logliklihood of data given a sample.
        This method does nothing in the base class.
        """
        return 0, 0
