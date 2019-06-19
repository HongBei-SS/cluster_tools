#! /usr/bin/python

import os
import sys
import json

import luigi
import numpy as np
import nifty.tools as nt

import cluster_tools.utils.volume_utils as vu
import cluster_tools.utils.function_utils as fu
from cluster_tools.cluster_tasks import SlurmTask, LocalTask, LSFTask


class BackgroundSizeFilterBase(luigi.Task):
    """ BackgroundSizeFilter base class
    """

    task_name = 'background_size_filter'
    src_file = os.path.abspath(__file__)

    input_path = luigi.Parameter()
    input_key = luigi.Parameter()
    output_path = luigi.Parameter()
    output_key = luigi.Parameter()
    dependency = luigi.TaskParameter()

    def requires(self):
        return self.dependency

    def _parse_log(self, log_path):
        log_path = self.input().path
        lines = fu.tail(log_path, 3)
        lines = [' '.join(ll.split()[2:]) for ll in lines]
        # load
        if lines[0].startswith("saving results to"):
            path = lines[0].split()[-1]
            assert os.path.exists(path), path
            return path
        else:
            raise RuntimeError("Could not parse log file.")

    def run_impl(self):
        # get the global config and init configs
        shebang, block_shape, roi_begin, roi_end = self.global_config_values()
        self.init(shebang)

        # get shape and make block config
        shape = vu.get_shape(self.input_path, self.input_key)

        if self.n_retries == 0:
            block_list = vu.blocks_in_volume(shape, block_shape, roi_begin, roi_end)
        else:
            block_list = self.block_list
            self.clean_up_for_retry(block_list)

        n_jobs = min(len(block_list), self.max_jobs)

        chunks = tuple(bs // 2 for bs in block_shape)
        # require the output dataset
        with vu.file_reader(self.output_path) as f:
            if self.output_key in f:
                chunks = f[self.output_key].chunks
            f.require_dataset(self.output_key, shape=shape, chunks=chunks,
                              dtype='uint64', compression='gzip')

        # we don't need any additional config besides the paths
        res_path = self._parse_log(self.input().path)
        config = {"input_path": self.input_path, "input_key": self.input_key,
                  "output_path": self.output_path, "output_key": self.output_key,
                  "block_shape": block_shape, 'res_path': res_path}
        self._write_log('scheduling %i blocks to be processed' % len(block_list))

        # prime and run the jobs
        self.prepare_jobs(n_jobs, block_list, config)
        self.submit_jobs(n_jobs)

        # wait till jobs finish and check for job success
        self.wait_for_jobs()
        self.check_jobs(n_jobs)


class BackgroundSizeFilterLocal(BackgroundSizeFilterBase, LocalTask):
    """
    BackgroundSizeFilter on local machine
    """
    pass


class BackgroundSizeFilterSlurm(BackgroundSizeFilterBase, SlurmTask):
    """
    BackgroundSizeFilter on slurm cluster
    """
    pass


class BackgroundSizeFilterLSF(BackgroundSizeFilterBase, LSFTask):
    """
    BackgroundSizeFilter on lsf cluster
    """
    pass


#
# Implementation
#


def apply_block(block_id, blocking, ds_in, ds_out, discard_ids):
    fu.log("start processing block %i" % block_id)
    block = blocking.getBlock(block_id)
    bb = tuple(slice(b, e) for b, e in zip(block.begin, block.end))
    labels = ds_in[bb]

    # check if everything is ignore label
    if np.sum(labels) == 0:
        fu.log_block_success(block_id)
        return

    discard_mask = np.in1d(labels, discard_ids).reshape(labels.shape)
    # check if the discard-mask is empty
    if np.sum(discard_mask) == 0:
        ds_out[bb] = labels
        fu.log_block_success(block_id)
        return

    labels[discard_mask] = 0
    ds_out[bb] = labels
    fu.log_block_success(block_id)


def background_size_filter(job_id, config_path):
    fu.log("start processing job %i" % job_id)
    fu.log("reading config from %s" % config_path)

    # read the config
    with open(config_path) as f:
        config = json.load(f)
    input_path = config['input_path']
    input_key = config['input_key']
    output_path = config['output_path']
    output_key = config['output_key']
    block_list = config['block_list']
    block_shape = config['block_shape']
    res_path = config['res_path']

    # get the shape
    with vu.file_reader(input_path, 'r') as f:
        ds = f[input_key]
        shape = f[input_key].shape
    blocking = nt.blocking(roiBegin=[0, 0, 0],
                           roiEnd=list(shape),
                           blockShape=list(block_shape))

    discard_ids = np.load(res_path)
    fu.log("Discarding %i ids" % len(discard_ids))

    same_file = input_path == output_path
    in_place = same_file and input_key == output_key

    if in_place:
        with vu.file_reader(input_path) as f:
            ds = f[input_key]
            [apply_block(block_id, blocking, ds, ds, discard_ids)
             for block_id in block_list]
    elif same_file:
        with vu.file_reader(input_path) as f:
            ds_in = f[input_key]
            ds_out = f[output_key]
            [apply_block(block_id, blocking, ds_in, ds_out, discard_ids)
             for block_id in block_list]
    else:
        with vu.file_reader(input_path, 'r') as f_in, vu.file_reader(output_path) as f_out:
            ds_in = f_in[input_key]
            ds_out = f_out[output_key]
            [apply_block(block_id, blocking, ds_in, ds_out, discard_ids)
             for block_id in block_list]

    # copy the 'maxId' attribute if present
    if job_id == 0 and not in_place:
        with vu.file_reader(input_path, 'r') as f:
            attrs = f[input_key].attrs
            max_id = attrs.get('maxId', None)
        if max_id is not None:
            with vu.file_reader(output_path) as f:
                f[output_key].attrs['maxId'] = max_id

    fu.log_job_success(job_id)


if __name__ == '__main__':
    path = sys.argv[1]
    assert os.path.exists(path), path
    job_id = int(os.path.split(path)[1].split('.')[0].split('_')[-1])
    background_size_filter(job_id, path)
