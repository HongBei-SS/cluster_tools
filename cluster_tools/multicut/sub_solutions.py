#! /bin/python

import os
import sys
import json
from concurrent import futures

import numpy as np
import luigi
import z5py
import nifty.tools as nt
import nifty.distributed as ndist

import cluster_tools.utils.volume_utils as vu
import cluster_tools.utils.function_utils as fu
from cluster_tools.cluster_tasks import SlurmTask, LocalTask, LSFTask


#
# Multicut Tasks
#


class SubSolutionsBase(luigi.Task):
    """ SubSolutions base class
    """

    task_name = 'sub_solutions'
    src_file = os.path.abspath(__file__)

    # input volumes and graph
    problem_path = luigi.Parameter()
    scale = luigi.IntParameter()
    ws_path = luigi.Parameter()
    ws_key = luigi.Parameter()
    output_path = luigi.Parameter()
    output_key = luigi.Parameter()
    # we have extra roi parameters here,
    # to quickly inspect a roi for debugging independent
    # of the roi of the global conifg
    # this roi must be inside of the global config's roi though.
    roi_begin = luigi.ListParameter(default=None)
    roi_end = luigi.ListParameter(default=None)
    # identifiers for the sub-result and sub-graph sub-keys
    sub_graph_identifier = luigi.Parameter('sub_graphs')
    sub_result_identifier = luigi.Parameter('sub_results')
    dependency = luigi.TaskParameter()

    def requires(self):
        return self.dependency

    def clean_up_for_retry(self, block_list):
        super().clean_up_for_retry(block_list)
        # TODO remove any output of failed blocks because it might be corrupted

    def run_impl(self):
        # get the global config and init configs
        shebang, block_shape, global_roi_begin, global_roi_end = self.global_config_values()
        self.init(shebang)

        assert (self.roi_begin is None) == (self.roi_end is None),\
            "Either both or neither of `roi_begin` and `roi_end` must be specified"
        # if we have don't jave a task-sppecific roi, set roi to global roi
        if self.roi_begin is None:
            roi_begin = global_roi_begin
            roi_end = global_roi_end
        else:
            # otherwise set to task-specific roi
            roi_begin = self.roi_begin
            roi_end = self.roi_end
            # if we also have a global roi, check that the task-specific roi
            # is in the global roi
            if global_roi_begin is not None:
                assert all(rb >= grb for rb, grb in zip(roi_begin, global_roi_begin))
            if global_roi_end is not None:
                assert all(re <= geb for re, geb in zip(roi_end, global_roi_end))

        # read shape
        with vu.file_reader(self.problem_path, 'r') as f:
            shape = tuple(f.attrs['shape'])

        # make output dataset
        with vu.file_reader(self.output_path) as f:
            f.require_dataset(self.output_key, shape=shape, dtype='uint64',
                              chunks=(25, 256, 256), compression='gzip')

        factor = 2**self.scale
        block_shape = tuple(bs * factor for bs in block_shape)

        # update the config with input and graph paths and keys
        # as well as block shape
        config = self.get_task_config()
        config.update({'problem_path': self.problem_path, 'scale': self.scale,
                       'block_shape': block_shape,
                       'ws_path': self.ws_path, 'ws_key': self.ws_key,
                       'output_path': self.output_path, 'output_key': self.output_key,
                       'sub_graph_identifier': self.sub_graph_identifier,
                       'sub_result_identifier': self.sub_result_identifier})

        if self.n_retries == 0:
            block_list = vu.blocks_in_volume(shape, block_shape, roi_begin, roi_end)
        else:
            block_list = self.block_list
            self.clean_up_for_retry(block_list)

        # prime and run the jobs
        prefix = 's%i' % self.scale
        self.prepare_jobs(1, block_list, config, prefix)
        self.submit_jobs(1, prefix)

        # wait till jobs finish and check for job success
        self.wait_for_jobs()
        self.check_jobs(1, prefix)

    # part of the luigi API
    def output(self):
        return luigi.LocalTarget(os.path.join(self.tmp_folder,
                                              self.task_name + '_s%i.log' % self.scale))


class SubSolutionsLocal(SubSolutionsBase, LocalTask):
    """ SubSolutions on local machine
    """
    pass


class SubSolutionsSlurm(SubSolutionsBase, SlurmTask):
    """ SubSolutions on slurm cluster
    """
    pass


class SubSolutionsLSF(SubSolutionsBase, LSFTask):
    """ SubSolutions on lsf cluster
    """
    pass


#
# Implementation
#


def _read_subresults(ds_results, block_node_prefix, blocking,
                     block_list, n_threads, initial_node_labeling=None):

    def read_subres(block_id):
        block = blocking.getBlock(block_id)
        # load nodes corresponding to this block
        block_path = block_node_prefix + str(block_id)
        nodes = ndist.loadNodes(block_path)
        # load the sub result for this block
        chunk = tuple(beg // bs for beg, bs in zip(block.begin, blocking.blockShape))
        subres = ds_results.read_chunk(chunk)

        # subres is None -> this block has ignore label
        # and has no edgees. Note that this does not imply that the
        # block ONLY has ignore label (or only one ordinary node)
        # because multiple ordinary nodes could be seperated by the ignore label
        # and thus not share an edge.
        if subres is None:
            assert 0 in nodes
            return None

        assert len(nodes) == len(subres), "block %i: %i, %i" % (block_id,
                                                                len(nodes),
                                                                len(subres))
        return nodes, subres, int(subres.max()) + 1

    with futures.ThreadPoolExecutor(n_threads) as tp:
        tasks = [tp.submit(read_subres, block_id) for block_id in block_list]
        results = [t.result() for t in tasks]

    # filter and get results
    block_list = [block_id for block_id, res
                  in zip(block_list, results) if res is not None]
    block_nodes = [res[0] for res in results if res is not None]
    block_res = [res[1] for res in results if res is not None]
    block_offsets = np.array([res[2] for res
                              in results if res is not None], dtype='uint64')

    # get the offsets and add them to the block results to make these unique
    block_offsets = np.roll(block_offsets, 1)
    block_offsets[0] = 0
    block_offsets = np.cumsum(block_offsets)
    block_res = [bres + boff for bres, boff in zip(block_res, block_offsets)]

    # apply the node labeling
    if initial_node_labeling is not None:
        fu.log("Apply initial node labeling to block nodes")
        block_nodes = [initial_node_labeling[nodes] for nodes in block_nodes]

    # construct result dicts for each block
    # keep zero mapped to zero
    block_results = [{node_id: res_id if node_id != 0 else 0
                      for node_id, res_id in zip(bnodes, bres)}
                     for bnodes, bres in zip(block_nodes, block_res)]
    return block_list, block_results


def _write_block_res(ds_in, ds_out,
                     block_id, blocking, block_res):
    fu.log("start processing block %i" % block_id)
    block = blocking.getBlock(block_id)
    bb = vu.block_to_bb(block)
    ws = ds_in[bb]

    seg = nt.takeDict(block_res, ws)
    ds_out[bb] = seg
    fu.log_block_success(block_id)


def sub_solutions(job_id, config_path):

    fu.log("start processing job %i" % job_id)
    fu.log("reading config from %s" % config_path)

    # get the config
    with open(config_path) as f:
        config = json.load(f)
    # input configs
    problem_path = config['problem_path']
    scale = config['scale']
    block_shape = config['block_shape']
    block_list = config['block_list']
    n_threads = config['threads_per_job']
    output_path = config['output_path']
    output_key = config['output_key']
    ws_path = config['ws_path']
    ws_key = config['ws_key']

    sub_result_identifier = config.get('sub_result_identifier', 'sub_results')
    sub_graph_identifier = config.get('sub_graph_identifier', 'sub_graphs')

    fu.log("reading problem from %s" % problem_path)
    problem = z5py.N5File(problem_path)
    shape = problem.attrs['shape']

    blocking = nt.blocking([0, 0, 0], list(shape), list(block_shape))

    # we need to project the ws labels back to the original labeling
    # for this, we first need to load the initial node labeling
    if scale > 1:
        node_label_key = 's%i/node_labeling' % scale
        fu.log("scale %i > 1; reading node labeling from %s" % (scale, node_label_key))
        ds_node_labeling = problem[node_label_key]
        ds_node_labeling.n_threads = n_threads
        initial_node_labeling = ds_node_labeling[:]
    else:
        initial_node_labeling = None

    # read the sub results
    ds_results = problem['s%i/%s/node_result' % (scale, sub_result_identifier)]
    # TODO should be varlen dataset
    fu.log("reading subresults")
    block_node_prefix = os.path.join(problem_path, 's%i' % scale, sub_graph_identifier, 'block_')
    block_list, block_results = _read_subresults(ds_results, block_node_prefix, blocking,
                                                 block_list, n_threads, initial_node_labeling)

    fu.log("writing subresults")
    # write the resulting segmentation
    with vu.file_reader(output_path) as f_out, vu.file_reader(ws_path, 'r') as f_in:
        ds_in = f_in[ws_key]
        ds_out = f_out[output_key]
        with futures.ThreadPoolExecutor(n_threads) as tp:
            tasks = [tp.submit(_write_block_res, ds_in, ds_out,
                               block_id, blocking, block_res)
                     for block_id, block_res in zip(block_list, block_results)]
            [t.result() for t in tasks]
    fu.log_job_success(job_id)


if __name__ == '__main__':
    path = sys.argv[1]
    assert os.path.exists(path), path
    job_id = int(os.path.split(path)[1].split('.')[0].split('_')[-1])
    sub_solutions(job_id, path)
