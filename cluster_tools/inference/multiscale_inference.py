#! /bin/python

import os
import sys
import json

import luigi
import dask
import numpy as np
import toolz as tz
import nifty.tools as nt

import cluster_tools.utils.volume_utils as vu
import cluster_tools.utils.function_utils as fu
from cluster_tools.utils.task_utils import DummyTask
from cluster_tools.cluster_tasks import SlurmTask, LocalTask, LSFTask
from cluster_tools.inference.frameworks import get_predictor, get_preprocessor
from cluster_tools.inference.inference import get_prep_model, _to_uint8


#
# Inference Tasks
#


class MultiscaleInferenceBase(luigi.Task):
    """ MultiscaleInference base class
    """

    task_name = 'multiscale_inference'
    src_file = os.path.abspath(__file__)
    allow_retry = False

    # input volume, output volume and inference parameter
    input_path = luigi.Parameter()
    input_key = luigi.Parameter()
    input_scales = luigi.ListParameter()
    scale_halos = luigi.ListParameter()
    scale_factors = luigi.ListParameter()

    output_path = luigi.Parameter()
    output_key = luigi.DictParameter()
    checkpoint_path = luigi.Parameter()
    halo = luigi.ListParameter()
    mask_path = luigi.Parameter(default='')
    mask_key = luigi.Parameter(default='')
    framework = luigi.Parameter(default='pytorch')
    #
    dependency = luigi.TaskParameter(default=DummyTask())

    def requires(self):
        return self.dependency

    @staticmethod
    def default_task_config():
        # we use this to get also get the common default config
        config = LocalTask.default_task_config()
        config.update({'dtype': 'uint8', 'compression': 'gzip', 'chunks': None,
                       'gpu_type': '2080Ti', 'device_mapping': None,
                       'use_best': True, 'prep_model': None, 'channel_accumulation': None})
        return config

    def run_impl(self):
        assert self.framework in ('pytorch', 'inferno')

        # TODO support ROI
        # shebang, block_shape, roi_begin, roi_end = self.global_config_values()
        shebang, block_shape = self.global_config_values()[:2]
        self.init(shebang)

        # load the task config
        config = self.get_task_config()
        dtype = config.pop('dtype', 'uint8')
        compression = config.pop('compression', 'gzip')
        chunks = config.pop('chunks', None)
        assert dtype in ('uint8', 'float32')

        # check th input datasets
        with vu.file_reader(self.input_path, 'r') as f:
            g = f[self.input_key]
            for ii, scale in enumerate(self.input_scales):
                assert scale in g
                if ii == 0:
                    shape = g[scale].shape
        n_scales = len(self.input_scales)
        assert len(self.scale_halos) == n_scales - 1
        assert len(self.scale_factors) == n_scales - 1

        # get shapes and chunks
        chunks = tuple(chunks) if chunks is not None else tuple(bs // 2 for bs in block_shape)
        # make sure block shape can be divided by chunks
        assert all(bs % ch == 0 for ch, bs in zip(chunks, block_shape)),\
            "%s, %s" % (str(chunks), block_shape)

        # TODO support output at multiple scales
        # check if we have single dataset or multi dataset output
        out_key_dict = self.output_key
        output_keys = list(out_key_dict.keys())
        output_params = list(out_key_dict.values())

        channel_accumulation = config.get('channel_accumulation', None)

        # make output volumes
        with vu.file_reader(self.output_path) as f:
            for out_key, out_channels in zip(output_keys, output_params):
                assert len(out_channels) == 2
                n_channels = out_channels[1] - out_channels[0]
                assert n_channels > 0
                if n_channels > 1 and channel_accumulation is None:
                    out_shape = (n_channels,) + shape
                    out_chunks = (1,) + chunks
                else:
                    out_shape = shape
                    out_chunks = chunks

                f.require_dataset(out_key, shape=out_shape,
                                  chunks=out_chunks, dtype=dtype, compression=compression)

        # update the config
        config.update({'input_path': self.input_path, 'input_key': self.input_key,
                       'input_scales': self.input_scales, 'scale_halos': self.scale_halos,
                       'scale_factors': self.scale_factors,
                       'output_path': self.output_path, 'checkpoint_path': self.checkpoint_path,
                       'block_shape': block_shape, 'halo': self.halo,
                       'output_keys': output_keys, 'channel_mapping': output_params,
                       'framework': self.framework})
        if self.mask_path != '':
            assert self.mask_key != ''
            config.update({'mask_path': self.mask_path, 'mask_key': self.mask_key})

        if self.n_retries == 0:
            # TODO support roi
            block_list = vu.blocks_in_volume(shape, block_shape)
        else:
            block_list = self.block_list
            self.clean_up_for_retry(block_list)

        n_jobs = min(len(block_list), self.max_jobs)
        # prime and run the jobs
        self.prepare_jobs(n_jobs, block_list, config)
        self.submit_jobs(n_jobs)

        # wait till jobs finish and check for job success
        self.wait_for_jobs()
        self.check_jobs(n_jobs)


class MultiscaleInferenceLocal(MultiscaleInferenceBase, LocalTask):
    """ Inference on local machine
    """
    pass


class MultiscaleInferenceSlurm(MultiscaleInferenceBase, SlurmTask):
    """ Inference on slurm cluster
    """
    def _write_slurm_file(self, job_prefix=None):
        groupname = self.get_global_config().get('groupname', 'kreshuk')
        # read and parse the relevant task config
        task_config = self.get_task_config()
        n_threads = task_config.get("threads_per_job", 1)
        time_limit = self._parse_time_limit(task_config.get("time_limit", 60))
        mem_limit = self._parse_mem_limit(task_config.get("mem_limit", 2))
        gpu_type = task_config.get('gpu_type', '2080Ti')

        # get file paths
        trgt_file = os.path.join(self.tmp_folder, self.task_name + '.py')
        config_tmpl = self._config_path('$1', job_prefix)
        job_name = self.task_name if job_prefix is None else '%s_%s' % (self.task_name, job_prefix)
        slurm_template = ("#!/bin/bash\n"
                          "#SBATCH -A %s\n"
                          "#SBATCH -N 1\n"
                          "#SBATCH -n %i\n"
                          "#SBATCH --mem %s\n"
                          "#SBATCH -t %s\n"
                          '#SBATCH -p gpu\n'
                          '#SBATCH -C gpu=%s\n'
                          '#SBATCH --gres=gpu:1\n'
                          "%s %s") % (groupname, n_threads,
                                      mem_limit, time_limit,
                                      gpu_type,
                                      trgt_file, config_tmpl)
        script_path = os.path.join(self.tmp_folder, 'slurm_%s.sh' % job_name)
        with open(script_path, 'w') as f:
            f.write(slurm_template)


class MultiscaleInferenceLSF(MultiscaleInferenceBase, LSFTask):
    """ Inference on lsf cluster
    """
    pass


#
# Implementation
#


def _load_input(ds, offset, block_shape, halo, scale_factor, scale_halo, padding_mode='reflect'):
    shape = ds.shape
    this_offset = [off // sf for off, sf in zip(offset, scale_factor)]
    this_block_shape = [bs // sf for bs, sf in zip(block_shape, scale_factor)]
    this_halo = [ha // sf for ha, sf in zip(halo, scale_factor)]

    starts = [off - ha - sh for off, ha, sh in zip(this_offset, this_halo, scale_halo)]
    stops = [off + bs + ha + sh for off, bs, ha, sh in zip(this_offset, this_block_shape,
                                                           this_halo, scale_halo)]

    # we pad the input volume if necessary
    pad_left = None
    pad_right = None

    # check for padding to the left
    if any(start < 0 for start in starts):
        pad_left = tuple(abs(start) if start < 0 else 0 for start in starts)
        starts = [max(0, start) for start in starts]

    # check for padding to the right
    if any(stop > shape[i] for i, stop in enumerate(stops)):
        pad_right = tuple(stop - shape[i] if stop > shape[i] else 0 for i, stop in enumerate(stops))
        stops = [min(shape[i], stop) for i, stop in enumerate(stops)]

    bb = tuple(slice(start, stop) for start, stop in zip(starts, stops))
    data = ds[bb]

    # pad if necessary
    if pad_left is not None or pad_right is not None:
        pad_left = (0, 0, 0) if pad_left is None else pad_left
        pad_right = (0, 0, 0) if pad_right is None else pad_right
        pad_width = tuple((pl, pr) for pl, pr in zip(pad_left, pad_right))
        data = np.pad(data, pad_width, mode=padding_mode)

    return data


def _load_inputs(datasets, offset, block_shape, halo, scale_factors, scale_halos):
    data = [_load_input(ds, offset, block_shape, halo, sf, sh)
            for ds, sf, sh in zip(datasets, scale_factors, scale_halos)]
    return data


def _run_inference(blocking, block_list, halo, ds_in, ds_out, mask,
                   scale_factors, scale_halos,
                   preprocess, predict, channel_mapping,
                   channel_accumulation, n_threads):

    block_shape = blocking.blockShape
    dtypes = [dso.dtype for dso in ds_out]
    dtype = dtypes[0]
    assert all(dtp == dtype for dtp in dtypes)

    @dask.delayed
    def load_input(block_id):
        fu.log("start processing block %i" % block_id)
        block = blocking.getBlock(block_id)

        # if we have a mask, check if this block is in mask
        if mask is not None:
            bb = vu.block_to_bb(block)
            bb_mask = mask[bb]
            if np.sum(bb_mask) == 0:
                return block_id, None

        return block_id, _load_inputs(ds_in, block.begin, block_shape, halo,
                                      scale_factors, scale_halos)

    @dask.delayed
    def preprocess_impl(inputs):
        block_id, data = inputs
        if data is None:
            return block_id, None
        data = preprocess(data)
        return block_id, data

    @dask.delayed
    def predict_impl(inputs):
        block_id, data = inputs
        if data is None:
            return block_id, None
        data = predict(data)
        return block_id, data

    @dask.delayed
    def write_output(inputs):
        block_id, output = inputs

        if output is None:
            return block_id

        out_shape = output.shape
        if len(out_shape) == 3:
            assert len(ds_out) == 1
        bb = vu.block_to_bb(blocking.getBlock(block_id))

        # check if we need to crop the output
        actual_shape = [b.stop - b.start for b in bb]
        if actual_shape != block_shape:
            block_bb = tuple(slice(0, ash) for ash in actual_shape)
            if output.ndim == 4:
                block_bb = (slice(None),) + block_bb
            output = output[block_bb]

        # write the output to our output dataset(s)
        for dso, chann_mapping in zip(ds_out, channel_mapping):
            chan_start, chan_stop = chann_mapping

            if dso.ndim == 3:
                if channel_accumulation is None:
                    assert chan_stop - chan_start == 1
                out_bb = bb
            else:
                assert output.ndim == 4
                assert chan_stop - chan_start == dso.shape[0]
                out_bb = (slice(None),) + bb

            if output.ndim == 4:
                channel_output = output[chan_start:chan_stop].squeeze()
            else:
                channel_output = output

            # apply channel accumulation if specified
            if channel_accumulation is not None and channel_output.ndim == 4:
                channel_output = channel_accumulation(channel_output, axis=0)

            # cast to uint8 if necessary
            if dtype == 'uint8':
                channel_output = _to_uint8(channel_output)

            dso[out_bb] = channel_output

        return block_id

    @dask.delayed
    def log2(block_id):
        fu.log_block_success(block_id)
        return 1

    # iterate over the blocks in block list, get the input data and predict
    results = []
    for block_id in block_list:
        res = tz.pipe(block_id, load_input,
                      preprocess_impl, predict_impl,
                      write_output, log2)
        results.append(res)

    success = dask.compute(*results, scheduler='threads', num_workers=n_threads)
    fu.log('Finished prediction for %i blocks' % sum(success))


def multiscale_inference(job_id, config_path):

    fu.log("start processing job %i" % job_id)
    fu.log("reading config from %s" % config_path)

    # get the config
    with open(config_path) as f:
        config = json.load(f)
    input_path = config['input_path']
    input_key = config['input_key']

    input_scales = config['input_scales']
    scale_factors = config['scale_factors']
    scale_halos = config['scale_halos']

    scale_factors = [[1, 1, 1]] + scale_factors
    scale_halos = [[0, 0, 0]] + scale_halos
    assert len(scale_factors) == len(scale_halos) == 3

    output_path = config['output_path']
    checkpoint_path = config['checkpoint_path']
    block_shape = config['block_shape']
    block_list = config['block_list']
    halo = config['halo']
    framework = config['framework']
    n_threads = config['threads_per_job']
    use_best = config.get('use_best', True)
    channel_accumulation = config.get('channel_accumulation', None)
    if channel_accumulation is not None:
        fu.log("Accumulating channels with %s" % channel_accumulation)
        channel_accumulation = getattr(np, channel_accumulation)

    fu.log("run inference with framework %s, with %i threads" % (framework, n_threads))

    output_keys = config['output_keys']
    channel_mapping = config['channel_mapping']

    device_mapping = config.get('device_mapping', None)
    if device_mapping is not None:
        device_id = device_mapping[str(job_id)]
        os.environ['CUDA_VISIBLE_DEVICES'] = str(device_id)
        fu.log("setting cuda visible devices to %i" % device_id)
    gpu = 0

    fu.log("Loading model from %s" % checkpoint_path)
    prep_model = config.get('prep_model', None)
    if prep_model is not None:
        prep_model = get_prep_model(prep_model)

    predict = get_predictor(framework)(checkpoint_path, halo, gpu=gpu, prep_model=prep_model,
                                       use_best=use_best)
    fu.log("Have model")
    preprocess = get_preprocessor(framework)

    with vu.file_reader(input_path, 'r') as f_in, vu.file_reader(output_path) as f_out:

        g_in = f_in[input_key]
        ds_in = [g_in[in_scale] for in_scale in input_scales]
        ds_out = [f_out[key] for key in output_keys]

        shape = ds_in[0].shape
        blocking = nt.blocking(roiBegin=[0, 0, 0],
                               roiEnd=list(shape),
                               blockShape=list(block_shape))

        if 'mask_path' in config:
            mask = vu.load_mask(config['mask_path'], config['mask_key'], shape)
        else:
            mask = None
        _run_inference(blocking, block_list, halo, ds_in, ds_out, mask,
                       scale_factors, scale_halos,
                       preprocess, predict, channel_mapping,
                       channel_accumulation, n_threads)
    fu.log_job_success(job_id)


if __name__ == '__main__':
    path = sys.argv[1]
    assert os.path.exists(path), path
    job_id = int(os.path.split(path)[1].split('.')[0].split('_')[-1])
    multiscale_inference(job_id, path)