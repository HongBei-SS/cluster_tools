import os
import json
import luigi

from .. cluster_tasks import WorkflowBase
from ..utils import volume_utils as vu
from . import block_node_labels as label_tasks
from . import merge_node_labels as merge_tasks


class NodeLabelWorkflow(WorkflowBase):
    ws_path = luigi.Parameter()
    ws_key = luigi.Parameter()
    input_path = luigi.Parameter()
    input_key = luigi.Parameter()
    output_path = luigi.Parameter()
    output_key = luigi.Parameter()
    prefix = luigi.Parameter(default='')
    max_overlap = luigi.BoolParameter(default=True)
    ignore_label = luigi.IntParameter(default=None)
    serialize_counts = luigi.BoolParameter(default=False)

    def requires(self):
        label_task = getattr(label_tasks,
                             self._get_task_name('BlockNodeLabels'))
        tmp_key = 'label_overlaps_%s' % self.prefix
        dep = label_task(max_jobs=self.max_jobs,
                         tmp_folder=self.tmp_folder,
                         config_dir=self.config_dir,
                         dependency=self.dependency,
                         ws_path=self.ws_path,
                         ws_key=self.ws_key,
                         input_path=self.input_path,
                         input_key=self.input_key,
                         output_path=self.output_path,
                         output_key=tmp_key,
                         ignore_label=self.ignore_label)
        merge_task = getattr(merge_tasks,
                             self._get_task_name('MergeNodeLabels'))
        dep = merge_task(max_jobs=self.max_jobs,
                         tmp_folder=self.tmp_folder,
                         config_dir=self.config_dir,
                         dependency=dep,
                         input_path=self.output_path,
                         input_key=tmp_key,
                         output_path=self.output_path,
                         output_key=self.output_key,
                         max_overlap=self.max_overlap,
                         ignore_label=self.ignore_label,
                         serialize_counts=self.serialize_counts)
        return dep

    @staticmethod
    def get_config():
        configs = super(NodeLabelWorkflow, NodeLabelWorkflow).get_config()
        configs.update({'block_node_labels':
                        label_tasks.BlockNodeLabelsLocal.default_task_config(),
                        'merge_node_labels':
                        merge_tasks.MergeNodeLabelsLocal.default_task_config()})
        return configs
