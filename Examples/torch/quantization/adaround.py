#=============================================================================
#
#  @@-COPYRIGHT-START-@@
#
#  Copyright (c) 2021, Qualcomm Innovation Center, Inc. All rights reserved.
#
#  Redistribution and use in source and binary forms, with or without
#  modification, are permitted provided that the following conditions are met:
#
#  1. Redistributions of source code must retain the above copyright notice,
#     this list of conditions and the following disclaimer.
#
#  2. Redistributions in binary form must reproduce the above copyright notice,
#     this list of conditions and the following disclaimer in the documentation
#     and/or other materials provided with the distribution.
#
#  3. Neither the name of the copyright holder nor the names of its contributors
#     may be used to endorse or promote products derived from this software
#     without specific prior written permission.
#
#  THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
#  AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
#  IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
#  ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
#  LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
#  CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
#  SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
#  INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
#  CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
#  ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
#  POSSIBILITY OF SUCH DAMAGE.
#
#  SPDX-License-Identifier: BSD-3-Clause
#
#  @@-COPYRIGHT-END-@@
#
#=============================================================================
"""
This file demonstrates the use of quantization using AIMET Adaround
technique.
"""

import argparse
from datetime import datetime
import logging
import copy
import os
from typing import Tuple
from functools import partial
from torchvision import models
import torch
import torch.utils.data as torch_data

# imports for AIMET
from aimet_torch.quantsim import QuantizationSimModel
from aimet_torch.batch_norm_fold import fold_all_batch_norms
from aimet_torch.adaround.adaround_weight import Adaround, AdaroundParameters
import aimet_common
from aimet_common.defs import QuantScheme

# imports for data pipelines
from Examples.common import image_net_config
from Examples.torch.utils.image_net_evaluator import ImageNetEvaluator
from Examples.torch.utils.image_net_data_loader import ImageNetDataLoader

logger = logging.getLogger('TorchAdaround')
formatter = logging.Formatter('%(asctime)s : %(name)s - %(levelname)s - %(message)s')
logging.basicConfig(format=formatter)

###
# This script utilizes AIMET to apply Adaround on a resnet18 pretrained model with
# the ImageNet data set. It should re-create the same performance numbers as
# published in the AIMET release for the particular scenario as described below.

# Scenario parameters:
#    - AIMET quantization accuracy using simulation model
#       - Quant Scheme: 'tf_enhanced'
#       - rounding_mode: 'nearest'
#       - default_output_bw: 8, default_param_bw: 8
#       - Encoding compution with or without encodings file
#       - Encoding computation using 5 batches of data
#    - AIMET Adaround
#       - num of batches for adarounding: 5
#       - bitwidth for quantizing layer parameters: 4
#       - Quant Scheme: 'tf_enhanced'
#       - Remaining Parameters: default
#    - Input shape: [1, 3, 224, 224]
###

class ImageNetDataPipeline:
    """
    Provides APIs for model quantization using evaluation and finetuning.
    """

    def __init__(self, _config: argparse.Namespace):
        """
        :param _config:
        """
        self._config = _config


    def evaluate(self, model: torch.nn.Module, iterations: int = None, use_cuda: bool = False) -> float:
        """
        Evaluate the specified model using the specified number of samples from the validation set.

        :param model: The model to be evaluated.
        :param iterations: The number of batches of the dataset.
        :param use_cuda: If True then use a GPU for inference.
        :return: The accuracy for the sample with the maximum accuracy.
        """

        # your code goes here instead of the example from below

        evaluator = ImageNetEvaluator(self._config.dataset_dir, image_size=image_net_config.dataset['image_size'],
                                      batch_size=image_net_config.evaluation['batch_size'],
                                      num_workers=image_net_config.evaluation['num_workers'])

        return evaluator.evaluate(model, iterations, use_cuda)


def apply_adaround_and_find_quantized_accuracy(model: torch.nn.Module, evaluator: aimet_common.defs.EvalFunction, data_loader: torch_data.DataLoader, use_cuda: bool = False, logdir: str = '')->Tuple[torch.nn.Module, float]:

    """
    Quantizes the model using AIMET's adaround feature, and saves the model.

    :param model: The loaded model
    :param evaluator: The Eval function to use for evaluation
    :param dataloader: The dataloader to be passed into the AdaroundParameters api
    :param num_val_samples_per_class: No of samples to use from every class in
                                      computing encodings. Not used in pascal voc
                                      dataset
    :param use_cuda: The cuda device.
    :param logdir: Path to a directory for logging.
    :return: A tuple of quantized model and accuracy of model on this quantsim
    """

    BN_folded_model = copy.deepcopy(model)
    _ = fold_all_batch_norms(BN_folded_model, input_shapes=(1, 3, 224, 224))

    input_shape = (1, image_net_config.dataset['image_channels'],
                   image_net_config.dataset['image_width'],
                   image_net_config.dataset['image_height'],)
    if use_cuda:
        dummy_input = torch.rand(input_shape).cuda()

    else:
        dummy_input = torch.rand(input_shape)


    params = AdaroundParameters(data_loader=data_loader, num_batches=5)
    ada_model = Adaround.apply_adaround(model, dummy_input, params,
                                        path=logdir, filename_prefix='adaround', default_param_bw=8,
                                        default_quant_scheme=QuantScheme.post_training_tf_enhanced)

    quantsim = QuantizationSimModel(model=ada_model, dummy_input=dummy_input, quant_scheme=QuantScheme.post_training_tf_enhanced,
                                    rounding_mode='nearest', default_output_bw=8, default_param_bw=8,
                                    in_place=False)

    # Freezing the encodings. This ensures params with frozen encodings will not be computed and set again
    # when compute_encodings() is invoked again.
    quantsim.set_and_freeze_param_encodings(encoding_path=os.path.join(logdir, 'adaround.encodings'))
    quantsim.compute_encodings(forward_pass_callback=partial(evaluator, use_cuda=use_cuda),
                               forward_pass_callback_args=None)
    quantsim.export(path=logdir, filename_prefix='adaround_resnet', dummy_input=dummy_input.cpu())
    accuracy = evaluator(quantsim.model, use_cuda=use_cuda)

    return quantsim, accuracy

def quantize(config: argparse.Namespace):
    """
    1. Instantiates Data Pipeline for evaluation
    2. Loads the pretrained resnet18 Pytorch model
    3. Calculates Model accuracy
        3.1. Calculates floating point accuracy
        3.2. Calculates Quant Simulator accuracy
    4. Applies AIMET CLE and BC
        4.1. Applies AIMET CLE and calculates QuantSim accuracy
        4.2. Applies AIMET BC and calculates QuantSim accuracy

    :param config: This argparse.Namespace config expects following parameters:
                   tfrecord_dir: Path to a directory containing ImageNet TFRecords.
                                This folder should conatin files starting with:
                                'train*': for training records and 'validation*': for validation records
                   use_cuda: A boolean var to indicate to run the test on GPU.
                   logdir: Path to a directory for logging.
    """

    # Instantiate Data Pipeline for evaluation and training
    data_pipeline = ImageNetDataPipeline(config)

    # Load the pretrained resnet18 model
    model = models.resnet18(pretrained=True)
    if config.use_cuda:
        model.to(torch.device('cuda'))
    model = model.eval()

    # Calculate floating point accuracy
    accuracy = data_pipeline.evaluate(model, use_cuda=config.use_cuda)
    logger.info("Original Model Top-1 accuracy = %.2f", accuracy)


    # Quantization
    logger.info("Starting Model Quantization...")

    # Quantize the model using AIMET Adaround
    data_loader = ImageNetDataLoader(is_training=False, images_dir=config.dataset_dir, image_size=image_net_config.dataset['image_size']).data_loader
    _, accuracy = apply_adaround_and_find_quantized_accuracy(model=model, evaluator=data_pipeline.evaluate, data_loader=data_loader, use_cuda=config.use_cuda, logdir=config.logdir)

    # Log the accuracy of quantized model
    logger.info("Quantized Model Top-1 accuracy = %.2f", accuracy)
    logger.info("...Model Quantization Complete")


if __name__ == '__main__':
    default_logdir = os.path.join("benchmark_output", "adaround"+datetime.now().strftime("%Y-%m-%d-%H-%M-%S"))

    parser = argparse.ArgumentParser(description='Apply Adaround on pretrained ResNet18 model and evaluate on ImageNet dataset')

    parser.add_argument('--dataset_dir', type=str,
                        required=True,
                        help="Path to a directory containing ImageNet dataset.\n\
                              This folder should conatin at least 2 subfolders:\n\
                              'train': for training dataset and 'val': for validation dataset")
    parser.add_argument('--use_cuda', action='store_true',
                        required=True,
                        help='Add this flag to run the test on GPU.')

    parser.add_argument('--logdir', type=str,
                        default=default_logdir,
                        help="Path to a directory for logging.\
                              Default value is 'benchmark_output/weight_svd_<Y-m-d-H-M-S>'")

    parser.add_argument('--epochs', type=int,
                        default=15,
                        help="Number of epochs for finetuning.\n\
                              Default is 15")
    parser.add_argument('--learning_rate', type=float,
                        default=1e-2,
                        help="A float type learning rate for model finetuning.\n\
                              default is 0.01")
    parser.add_argument('--learning_rate_schedule', type=list,
                        default=[5, 10],
                        help="A list of epoch indices for learning rate schedule used in finetuning.\n\
                              Check https://pytorch.org/docs/stable/_modules/torch/optim/lr_scheduler.html#MultiStepLR for more details.\n\
                              default is [5, 10]")

    _config = parser.parse_args()

    os.makedirs(_config.logdir, exist_ok=True)

    fileHandler = logging.FileHandler(os.path.join(_config.logdir, "test.log"))
    fileHandler.setFormatter(formatter)
    logger.addHandler(fileHandler)

    if _config.use_cuda and not torch.cuda.is_available():
        logger.error('use_cuda is selected but no cuda device found.')
        raise RuntimeError("Found no CUDA Device while use_cuda is selected")

    quantize(_config)
