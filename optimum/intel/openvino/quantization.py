#  Copyright 2022 The HuggingFace Team. All rights reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

import copy
import inspect
import logging
import os
from collections import deque
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import nncf
import openvino
import torch
import transformers
from nncf import CompressWeightsMode, IgnoredScope, SensitivityMetric
from nncf.quantization.advanced_parameters import AdvancedSmoothQuantParameters
from nncf.torch import register_module
from nncf.torch.initialization import PTInitializingDataLoader
from openvino._offline_transformations import compress_quantize_weights_transformation
from openvino.runtime import Core, Tensor
from torch.utils._pytree import tree_map
from torch.utils.data import DataLoader, RandomSampler
from transformers import AutoTokenizer, DataCollator, PreTrainedModel, default_data_collator
from transformers.pytorch_utils import Conv1D
from transformers.utils import is_accelerate_available

from optimum.exporters.onnx.convert import check_dummy_inputs_are_allowed
from optimum.exporters.tasks import TasksManager
from optimum.quantization_base import OptimumQuantizer

from ...exporters.openvino import export, export_pytorch_via_onnx
from ...exporters.openvino.model_patcher import patch_model_with_bettertransformer
from ...exporters.openvino.stateful import ensure_export_task_support_stateful, ensure_stateful_is_available
from ..utils.constant import _TASK_ALIASES
from ..utils.import_utils import DATASETS_IMPORT_ERROR, is_datasets_available
from ..utils.modeling_utils import get_model_device
from .configuration import OVConfig, OVWeightQuantizationConfig
from .modeling_base import OVBaseModel
from .utils import (
    MAX_ONNX_OPSET,
    MIN_ONNX_QDQ_OPSET,
    ONNX_WEIGHTS_NAME,
    OV_XML_FILE_NAME,
)


if is_datasets_available():
    from datasets import Dataset

register_module(ignored_algorithms=[])(Conv1D)

core = Core()
logger = logging.getLogger(__name__)


class OVDataLoader(PTInitializingDataLoader):
    def get_inputs(self, dataloader_output) -> Tuple[Tuple, Dict]:
        return (), dataloader_output

    @property
    def batch_size(self):
        batch_size = self._data_loader.batch_size
        if is_accelerate_available():
            from accelerate.data_loader import DataLoaderStateMixin

            if batch_size is None and isinstance(self._data_loader, DataLoaderStateMixin):
                batch_size = self._data_loader.total_batch_size
        return batch_size


class InferRequestWrapper:
    """
    Wrapper class for OV InferRequest or CompiledModel objects that collects inputs which they were called with to
    a list.
    """

    def __init__(
        self,
        request: Union[openvino.InferRequest, openvino.CompiledModel],
        collected_inputs: List = None,
        apply_caching: bool = False,
    ):
        """
        Args:
            request (`Union[openvino.InferRequest, openvino.CompiledModel]`):
                Infer request instance to wrap. May also be an instance of CompiledModel.
            collected_inputs (`List`, *optional*):
                List where collected inputs will be stored. If None, an empty list will be created
                at self.collected_inputs.
            apply_caching (`bool`, defaults to False):
                Whether to apply data caching. May improve memory footprint, but results in slight performance overhead
                due to tensor hash computation.
        """
        self.request = request
        self.collected_inputs = [] if collected_inputs is None else collected_inputs
        self.apply_caching = apply_caching
        self.tensor_cache = {}

    def collect_inputs(self, inputs):
        if not self.apply_caching or not isinstance(inputs, dict):
            self.collected_inputs.append(copy.deepcopy(inputs))
            return

        copied_inputs = {}
        for k, v in inputs.items():
            data = v
            if isinstance(data, openvino.Tensor):
                data = data.data
            if isinstance(data, torch.Tensor):
                data = data.cpu().numpy()
            data_hash = hash(data.tobytes())

            # Avoid data copying if tensor contains data encountered earlier
            if data_hash not in self.tensor_cache:
                self.tensor_cache[data_hash] = copy.deepcopy(v)
            copied_inputs[k] = self.tensor_cache[data_hash]
        self.collected_inputs.append(copied_inputs)

    def __call__(self, *args, **kwargs):
        # If __call__ is invoked then self.request must be an instance of CompiledModel
        signature = inspect.signature(self.request)
        bound_args = signature.bind(*args, **kwargs).arguments
        self.collect_inputs(bound_args["inputs"])
        return self.request(*args, **kwargs)

    def infer(self, inputs: Any = None, share_inputs: bool = False):
        self.collect_inputs(inputs)
        return self.request.infer(inputs, share_inputs)

    def start_async(
        self,
        inputs: Any = None,
        userdata: Any = None,
        share_inputs: bool = False,
        *,
        shared_memory: Any = None,
    ):
        self.collect_inputs(inputs)
        self.request.infer(inputs, share_inputs, share_outputs=True)

    def wait(self):
        pass

    def get_tensor(self, name: str):
        return Tensor(self.request.results[name])

    def __getattr__(self, attr):
        if attr in self.__dict__:
            return getattr(self, attr)
        return getattr(self.request, attr)


class OVQuantizer(OptimumQuantizer):
    """
    Handle the NNCF quantization process.
    """

    def __init__(self, model: transformers.PreTrainedModel, task: Optional[str] = None, seed: int = 42, **kwargs):
        """
        Args:
            model (`transformers.PreTrainedModel`):
                The [PreTrainedModel](https://huggingface.co/docs/transformers/main_classes/model#transformers.PreTrainedModel) to quantize.
            task (`str`, defaults to None):
                The task defining the model topology used for the ONNX export.
            seed (`int`, defaults to 42):
                The random seed to use when shuffling the calibration dataset.
        """
        super().__init__()
        self.model = model
        feature = kwargs.pop("feature", None)
        if feature is not None:
            logger.warning("`feature` is deprecated and will be removed in a future version. Use `task` instead.")
        if task is not None and task != feature:
            logger.warning(
                f"Both `feature` and `task` were specified. {task} will be used to define the model topology for the model ONNX export."
            )
        self.task = task or feature
        self.seed = seed
        # TODO : deprecate input_names
        self.input_names = None
        signature = inspect.signature(self.model.forward)
        self._signature_columns = list(signature.parameters.keys())
        self._export_input_names = [
            column for column in self._signature_columns if column not in {"label", "labels", "label_ids"}
        ]

    @classmethod
    def from_pretrained(cls, model: PreTrainedModel, **kwargs):
        # TODO : Create model
        return cls(model, **kwargs)

    def quantize(
        self,
        calibration_dataset: "Dataset" = None,
        save_directory: Union[str, Path] = None,
        ov_config: OVConfig = None,
        file_name: Optional[str] = None,
        batch_size: int = 1,
        data_collator: Optional[DataCollator] = None,
        remove_unused_columns: bool = True,
        weights_only: bool = False,
        **kwargs,
    ):
        """
        Quantize a model given the optimization specifications defined in `quantization_config`.

        Args:
            calibration_dataset (`datasets.Dataset`):
                The dataset to use for the calibration step.
            save_directory (`Union[str, Path]`):
                The directory where the quantized model should be saved.
            quantization_config (`OVConfig`, *optional*):
                The configuration containing the parameters related to quantization.
            file_name (`str`, *optional*):
                The model file name to use when saving the model. Overwrites the default file name `"model.onnx"`.
            batch_size (`int`, defaults to 8):
                The number of calibration samples to load per batch.
            data_collator (`DataCollator`, *optional*):
                The function to use to form a batch from a list of elements of the calibration dataset.
            remove_unused_columns (`bool`, defaults to `True`):
                Whether or not to remove the columns unused by the model forward method.
            weights_only (`bool`, defaults to `False`):
                Compress weights to integer precision (8-bit by default) while keeping activations
                floating-point. Fits best for LLM footprint reduction and performance acceleration.

        Examples:
        ```python
        >>> from optimum.intel.openvino import OVQuantizer, OVModelForSequenceClassification
        >>> from transformers import AutoModelForSequenceClassification
        >>> model = OVModelForSequenceClassification.from_pretrained("distilbert-base-uncased-finetuned-sst-2-english", export=True)
        >>> # or
        >>> model = AutoModelForSequenceClassification.from_pretrained("distilbert-base-uncased-finetuned-sst-2-english")
        >>> quantizer = OVQuantizer.from_pretrained(model, task="text-classification")
        >>> quantizer.quantize(calibration_dataset=calibration_dataset, save_directory="./quantized_model")
        >>> optimized_model = OVModelForSequenceClassification.from_pretrained("./quantized_model")
        ```

        ```python
        >>> from optimum.intel.openvino import OVQuantizer, OVModelForCausalLM
        >>> from transformers import AutoModelForCausalLM
        >>> model = AutoModelForCausalLM.from_pretrained("databricks/dolly-v2-3b")
        >>> quantizer = OVQuantizer.from_pretrained(model, task="text-generation")
        >>> quantizer.quantize(save_directory="./quantized_model", weights_only=True)
        >>> optimized_model = OVModelForCausalLM.from_pretrained("./quantized_model")
        ```
        """
        if save_directory is None:
            # TODO : can be set to self.model.config.name_or_path for OVModels when not provided
            raise ValueError("`save_directory` needs to be specified")
        if weights_only:
            if calibration_dataset is not None:
                logger.warning(
                    "`calibration_dataset` was provided but will not be used as `weights_only` is set to `True`."
                )
        else:
            if calibration_dataset is None:
                raise ValueError(
                    "`calibration_dataset` is needed to compute the activations range during the calibration step and was not provided. "
                    "In case you only want to apply quantization on the weights, please set `weights_only=True`."
                )
        quantization_config = kwargs.pop("quantization_config", None)
        if quantization_config is not None:
            logger.warning(
                "The argument `quantization_config` is deprecated, and will be removed in optimum-intel v1.6.0, please use `ov_config` instead"
            )
        ov_config = ov_config or quantization_config

        if ov_config is not None:
            if not isinstance(ov_config, OVConfig):
                raise TypeError(f"`ov_config` should be an `OVConfig`, but got: {type(ov_config)} instead.")

        if isinstance(self.model, OVBaseModel):
            self._quantize_ovbasemodel(
                calibration_dataset,
                save_directory,
                batch_size,
                data_collator,
                remove_unused_columns,
                weights_only,
                ov_config,
                **kwargs,
            )

        elif isinstance(self.model, torch.nn.Module):
            logger.warning(
                "The support of `torch.nn.Module` will be deprecated in a future release of optimum-intel, please use the corresponding `OVModelForXxx` class to load you model."
                "To convert a PyTorch model to OpenVINO, you can set `export=True` when loading your model as `OVModelForXxx.from_pretrained(..., export=True)`"
            )
            self._quantize_torchmodel(
                calibration_dataset,
                save_directory,
                file_name,
                batch_size,
                data_collator,
                remove_unused_columns,
                weights_only,
            )
        else:
            raise TypeError(f"Unsupported model type: {type(self.model)}")

    def _quantize_ovbasemodel(
        self,
        calibration_dataset: "Dataset",
        save_directory: Union[str, Path],
        batch_size: int = 1,
        data_collator: Optional[DataCollator] = None,
        remove_unused_columns: bool = True,
        weights_only: bool = False,
        ov_config: OVConfig = None,
        **kwargs,
    ):
        save_directory = Path(save_directory)
        save_directory.mkdir(parents=True, exist_ok=True)

        if weights_only:
            q_config = getattr(ov_config, "quantization_config", None)
            # Use default 8-bit compression if not provided
            q_config = q_config or OVWeightQuantizationConfig(bits=8, sym=True)
            _weight_only_quantization(self.model.model, q_config)

            self.model.save_pretrained(save_directory)
            return

        calibration_dataloader = self._get_calibration_dataloader(
            calibration_dataset=calibration_dataset,
            batch_size=batch_size,
            remove_unused_columns=remove_unused_columns,
            data_collator=data_collator,
        )

        if self.model.export_feature == "text-generation" and self.model.use_cache:
            # Prefeth past_key_values
            self.model.update_pkv_precision(True)
            self.model.compile()
            subset_size = kwargs.get("subset_size", 300)
            collected_inputs = []

            self.model.request = InferRequestWrapper(self.model.request, collected_inputs)
            for _, data in enumerate(calibration_dataloader):
                self.model.generate(**data, max_new_tokens=1)
                if len(collected_inputs) >= subset_size:
                    break
            self.model.request = self.model.request.request
            calibration_dataloader = collected_inputs

        # Actual model quantization
        quantization_dataset = nncf.Dataset(calibration_dataloader)
        quantized_model = nncf.quantize(
            self.model.model,
            quantization_dataset,
            model_type=nncf.ModelType.TRANSFORMER if not kwargs.get("model_type") else kwargs.get("model_type"),
            fast_bias_correction=kwargs.get("fast_bias_correction", True),
            **kwargs,
        )
        self.model.model = quantized_model
        self.model.save_pretrained(save_directory)

    def _quantize_torchmodel(
        self,
        calibration_dataset: "Dataset",
        save_directory: Union[str, Path],
        file_name: Optional[str] = None,
        batch_size: int = 1,
        data_collator: Optional[DataCollator] = None,
        remove_unused_columns: bool = True,
        weights_only: bool = False,
        save_onnx_model: bool = False,
        **kwargs,
    ):
        self._set_task()
        save_directory = Path(save_directory)
        save_directory.mkdir(parents=True, exist_ok=True)
        ov_file_name = file_name if file_name is not None else OV_XML_FILE_NAME
        output_path = save_directory.joinpath(ov_file_name)
        output_path = output_path.with_suffix(".xml").as_posix()

        model_type = self.model.config.model_type.replace("_", "-")
        onnx_config_class = TasksManager.get_exporter_config_constructor(
            exporter="openvino",
            model=self.model,
            task=self.task,
            model_type=model_type,
        )

        onnx_file_name = (
            ONNX_WEIGHTS_NAME if file_name is None and save_onnx_model else Path(ov_file_name).with_suffix(".onnx")
        )

        task = self.task
        model = self.model
        self.model.config.save_pretrained(save_directory)
        if task.startswith("text-generation"):
            onnx_config = onnx_config_class(
                model.config, use_past=model.config.use_cache, use_past_in_inputs=model.config.use_cache
            )
            if model.config.use_cache:
                task = "text-generation-with-past"
        else:
            onnx_config = onnx_config_class(model.config)

        stateful = ensure_stateful_is_available() and ensure_export_task_support_stateful(task)

        if weights_only:
            if stateful:
                # patch model before weight compression
                model = patch_model_with_bettertransformer(model)

            dummy_inputs = onnx_config.generate_dummy_inputs(framework="pt")
            device = get_model_device(model)
            dummy_inputs = tree_map(
                lambda value: value.to(device) if isinstance(value, torch.Tensor) else value, dummy_inputs
            )
            check_dummy_inputs_are_allowed(model, dummy_inputs)

            nncf.compress_weights(model, dataset=nncf.Dataset([dummy_inputs]))
        else:
            if stateful:
                logger.warn(
                    "Quantization algorithm does not support optimized stateful models. "
                    "The original model without optimization will be quantized and exported."
                )
                stateful = False

            calibration_dataloader = self._get_calibration_dataloader(
                calibration_dataset=calibration_dataset,
                batch_size=batch_size,
                remove_unused_columns=remove_unused_columns,
                data_collator=data_collator,
            )

            quantization_dataset = nncf.Dataset(calibration_dataloader)
            model = nncf.quantize(
                model,
                quantization_dataset,
                model_type=nncf.ModelType.TRANSFORMER if not kwargs.get("model_type") else kwargs.get("model_type"),
                fast_bias_correction=kwargs.get("fast_bias_correction", True),
                **kwargs,
            )

        model_path = save_directory / (onnx_file_name if save_onnx_model else ov_file_name)
        onnx_path = save_directory / onnx_file_name
        export_fn = export if not save_onnx_model else export_pytorch_via_onnx
        opset = min(onnx_config.DEFAULT_ONNX_OPSET, MAX_ONNX_OPSET)
        opset = max(opset, MIN_ONNX_QDQ_OPSET)
        export_kwargs = {}
        if not save_onnx_model:
            export_kwargs = {"stateful": stateful}

        _, _, is_onnx = export_fn(model=model, config=onnx_config, output=model_path, opset=opset, **export_kwargs)
        if is_onnx:
            # Load and save the compressed model
            model = core.read_model(onnx_path)
            # Model required second saving for appling weights compression transformations
            self._save_pretrained(model, output_path)
            # if onnx conversion happens as fallback for pytorch conversion, remove onnx model
            if not save_onnx_model:
                os.remove(onnx_path)
                try:
                    os.remove(f"{onnx_path}_data")
                except FileNotFoundError:
                    pass

    @staticmethod
    def _save_pretrained(model: openvino.runtime.Model, output_path: str):
        compress_quantize_weights_transformation(model)
        openvino.save_model(model, output_path, compress_to_fp16=False)

    def _set_task(self):
        if self.task is None:
            self.task = TasksManager.infer_task_from_model(self.model.config._name_or_path)
            if self.task is None:
                raise ValueError(
                    "The task defining the model topology could not be extracted and needs to be specified for the ONNX export."
                )

        self.task = _TASK_ALIASES.get(self.task, self.task)

        if self.task == "text2text-generation":
            raise ValueError("Seq2Seq models are currently not supported for post-training static quantization.")

        if self.task == "image-to-text":
            raise ValueError("Image2Text models are currently not supported for post-training static quantization.")

    def get_calibration_dataset(
        self,
        dataset_name: str,
        num_samples: int = 100,
        dataset_config_name: Optional[str] = None,
        dataset_split: str = "train",
        preprocess_function: Optional[Callable] = None,
        preprocess_batch: bool = True,
        use_auth_token: bool = False,
        cache_dir: Optional[str] = None,
    ) -> "Dataset":
        """
        Create the calibration `datasets.Dataset` to use for the post-training static quantization calibration step.

        Args:
            dataset_name (`str`):
                The dataset repository name on the Hugging Face Hub or path to a local directory containing data files
                in generic formats and optionally a dataset script, if it requires some code to read the data files.
            num_samples (`int`, defaults to 100):
                The maximum number of samples composing the calibration dataset.
            dataset_config_name (`str`, *optional*):
                The name of the dataset configuration.
            dataset_split (`str`, defaults to `"train"`):
                Which split of the dataset to use to perform the calibration step.
            preprocess_function (`Callable`, *optional*):
                Processing function to apply to each example after loading dataset.
            preprocess_batch (`bool`, defaults to `True`):
                Whether the `preprocess_function` should be batched.
            use_auth_token (`bool`, defaults to `False`):
                Whether to use the token generated when running `transformers-cli login`.
            cache_dir (`str`, *optional*):
                Caching directory for a calibration dataset.
        Returns:
            The calibration `datasets.Dataset` to use for the post-training static quantization calibration step.
        """
        if not is_datasets_available():
            raise ValueError(DATASETS_IMPORT_ERROR.format("OVQuantizer.get_calibration_dataset"))
        from datasets import load_dataset

        calibration_dataset = load_dataset(
            dataset_name,
            name=dataset_config_name,
            split=dataset_split,
            use_auth_token=use_auth_token,
            cache_dir=cache_dir,
        )

        if num_samples is not None:
            num_samples = min(num_samples, len(calibration_dataset))
            calibration_dataset = calibration_dataset.shuffle(seed=self.seed).select(range(num_samples))

        if preprocess_function is not None:
            calibration_dataset = calibration_dataset.map(preprocess_function, batched=preprocess_batch)

        return calibration_dataset

    def _get_calibration_dataloader(
        self,
        calibration_dataset: "Dataset",
        batch_size: int,
        remove_unused_columns: bool,
        data_collator: Optional[DataCollator] = None,
    ) -> OVDataLoader:
        data_collator = data_collator if data_collator is not None else default_data_collator

        if not is_datasets_available() or not isinstance(calibration_dataset, Dataset):
            logger.warning(
                "`remove_unused_columns` set to `False` as calibration_dataset is not an instance of `datasets.Dataset`"
            )
            remove_unused_columns = False

        if remove_unused_columns:
            calibration_dataset = self._remove_unused_columns(calibration_dataset)
        generator = torch.Generator()
        generator.manual_seed(self.seed)
        sampler = RandomSampler(calibration_dataset, generator=generator)
        calibration_dataloader = DataLoader(
            calibration_dataset, batch_size=batch_size, sampler=sampler, collate_fn=data_collator, drop_last=False
        )
        return OVDataLoader(calibration_dataloader)

    def _remove_unused_columns(self, dataset: "Dataset"):
        ignored_columns = list(set(dataset.column_names) - set(self._signature_columns))
        return dataset.remove_columns(ignored_columns)


def _weight_only_quantization(
    model: openvino.runtime.Model, quantization_config: Union[OVWeightQuantizationConfig, Dict]
) -> openvino.runtime.Model:
    config = quantization_config
    if isinstance(config, dict):
        config = OVWeightQuantizationConfig.from_dict(quantization_config)

    dataset = config.dataset

    if config.dataset is not None and isinstance(config.dataset, str):
        tokenizer = config.tokenizer
        if isinstance(tokenizer, str):
            tokenizer = AutoTokenizer.from_pretrained(tokenizer)

        from optimum.gptq.data import get_dataset, prepare_dataset

        nsamples = config.num_samples if config.num_samples else 128
        dataset = get_dataset(config.dataset, tokenizer, seqlen=32, nsamples=nsamples)
        dataset = prepare_dataset(dataset)

    sensitivity_metric = None
    if isinstance(config.sensitivity_metric, str):
        sensitivity_metric = getattr(SensitivityMetric, config.sensitivity_metric.upper())

    ignored_scope = None
    if isinstance(config.ignored_scope, dict):
        ignored_scope = IgnoredScope(**config.ignored_scope)

    if config.bits == 8:
        mode = CompressWeightsMode.INT8_SYM if config.sym else CompressWeightsMode.INT8_ASYM
    else:
        mode = CompressWeightsMode.INT4_SYM if config.sym else CompressWeightsMode.INT4_ASYM

    return nncf.compress_weights(
        model,
        mode=mode,
        ratio=config.ratio,
        group_size=config.group_size,
        all_layers=config.all_layers,
        sensitivity_metric=sensitivity_metric,
        # awq=config.quant_method == "awq", # TODO : remove and add it back once nncf v2.9.0
        ignored_scope=ignored_scope,
        dataset=dataset,
        # subset_size=config.num_samples if config.num_samples else 128, # TODO : enable from nncf v2.9.0
    )


def _get_operation_const_op(operation, const_port_id: int):
    node = operation.input_value(const_port_id).get_node()
    queue = deque([node])
    constant_node = None
    allowed_propagation_types_list = ["Convert", "FakeQuantize", "Reshape"]

    while len(queue) != 0:
        curr_node = queue.popleft()
        if curr_node.get_type_name() == "Constant":
            constant_node = curr_node
            break
        if len(curr_node.inputs()) == 0:
            break
        if curr_node.get_type_name() in allowed_propagation_types_list:
            queue.append(curr_node.input_value(0).get_node())

    return constant_node


def _is_embedding(node) -> bool:
    allowed_types_list = ["f16", "f32", "f64"]
    const_port_id = 0
    input_tensor = node.input_value(const_port_id)
    if input_tensor.get_element_type().get_type_name() in allowed_types_list:
        const_node = _get_operation_const_op(node, const_port_id)
        if const_node is not None:
            return True

    return False


def _collect_ops_with_weights(model):
    ops_with_weights = []
    for op in model.get_ops():
        if op.get_type_name() == "MatMul":
            constant_node_0 = _get_operation_const_op(op, const_port_id=0)
            constant_node_1 = _get_operation_const_op(op, const_port_id=1)
            if constant_node_0 or constant_node_1:
                ops_with_weights.append(op.get_friendly_name())
        if op.get_type_name() == "Gather" and _is_embedding(op):
            ops_with_weights.append(op.get_friendly_name())

    return ops_with_weights


def _hybrid_quantization(
    model: openvino.runtime.Model, quantization_config: OVWeightQuantizationConfig, dataset: Dict[str, Any]
) -> openvino.runtime.Model:
    """
    Quantize a model in hybrid mode with NNCF which means that we quantize:
    weights of MatMul and Embedding layers and activations of other layers.
    The optimization specifications defined in `quantization_config`.

    Args:
        model (`openvino.runtime.Model`):
            The OpenVINO Runtime model for applying hybrid quantization.
        quantization_config (`OVWeightQuantizationConfig`):
            The configuration containing the parameters related to quantization.
        dataset (`Dict[str, Any]`):
            The dataset used for hybrid quantization.
    Returns:
        The OpenVINO Runtime model with applied hybrid quantization.
    """
    ops_to_compress = _collect_ops_with_weights(model)

    ignored_scope = quantization_config.ignored_scope if isinstance(quantization_config.ignored_scope, dict) else {}
    ptq_ignored_scope = nncf.IgnoredScope(**ignored_scope)
    ptq_ignored_scope.names += ops_to_compress

    wc_quantization_config = copy.deepcopy(quantization_config)
    wc_quantization_config.ignored_scope = ignored_scope
    wc_quantization_config.ignored_scope["types"] = ignored_scope.get("types", []) + ["Convolution"]
    compressed_model = _weight_only_quantization(model, wc_quantization_config)

    subset_size = quantization_config.num_samples if quantization_config.num_samples else 200
    quantized_model = nncf.quantize(
        model=compressed_model,
        calibration_dataset=nncf.Dataset(dataset),
        model_type=nncf.ModelType.TRANSFORMER,
        ignored_scope=ptq_ignored_scope,
        # The SQ algo should be disabled for MatMul nodes because their weights are already compressed
        advanced_parameters=nncf.AdvancedQuantizationParameters(AdvancedSmoothQuantParameters(matmul=-1)),
        subset_size=subset_size,
    )
    return quantized_model
