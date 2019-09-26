from __future__ import print_function

import torch
import tqdm
import time

from collections import OrderedDict
from torch import nn
from tqdm.auto import tqdm as auto_tqdm

from .training_callbacks import NumberOfEpochsStoppingCriterionCallback, StoppingCriterionCallback


NCOLS = 80 if auto_tqdm is tqdm.std.tqdm else None


class System(object):
    """
    A system contains the usual methods needed for a deep learning model (train, evaluate, predict, save, load, etc).
    """

    def __init__(self, model, last_activation=None, device=torch.device('cpu')):
        """
        :param model: An nn.Module object that represents the whole model. The module's forward method must return a
            Tensor or a Dict of Tensors.
        :param last_activation: Callable that needs to be called at non train time. Some losses work with logits and as
            such the last activation might not be performed inside the model's forward method. If the last activation is
            performed inside the model then pass None.
        :param device: Device on which the model should reside.
        """

        self.model = model
        self.last_activation = last_activation
        self.model = self.model.to(device)
        self.model.train(False)
        self._device = device

    @property
    def device(self):
        return self._device

    def train_on_multi_gpus(self,
                            loss_wrapper,
                            optimizer,
                            train_data_loader,
                            evaluation_data_loaders=None,
                            batch_input_key='input',
                            evaluators=None,
                            callbacks=None,
                            gradient_accumulation_steps=1,
                            multi_gpu_device_ids=None,
                            multi_gpu_output_device=None,
                            multi_gpu_dim=0):
        """
        Trains the model on a dataset using multiple GPUs. At the end of training the model is moved back to the device
        it was on at the beginning.

        :param loss_wrapper: Object derived from AbstractLossWrapper that wraps the calculation of the loss.
        :param optimizer: Optimizer object.
        :param train_data_loader: DataLoader object that generates batches of the train dataset. Each batch must be a
            Dict that contains at least a Tensor or a list/tuple of Tensors containing the input(s) of the model
            (key=`batch_input_key`).
        :param evaluation_data_loaders: Dictionary containing the evaluation data-loaders. The keys are the datasets'
            names. Each batch generated by the dataloaders must be a  Dict that contains the input of the model
            (key=`batch_input_key`) as well as the information needed by the evaluators.
        :param batch_input_key: Key of the Dicts returned by the Dataloader objects that corresponds to the input of the
            model.
        :param evaluators: Dictionary containing objects derived from AbstractEvaluator. The keys are the evaluators'
            names.
        :param callbacks: List containing TrainingCallback objects. They are used in order to inject functionality at
            several points of the training process. Default is NumberOfEpochsStoppingCriterionCallback(10) that stops
            training after the 10th iteration (counting from 0).
        :param gradient_accumulation_steps: Number of backward calls before an optimization step. Used in order to
            simulate a larger batch size).
        :param multi_gpu_device_ids: CUDA devices used during training (default: all devices).
        :param multi_gpu_output_device: Device location of output (default: device_ids[0]).
        :param multi_gpu_dim: Int dimension on which to split each batch.
        :return: List containing the results for each epoch.
        """

        assert torch.cuda.is_available(), 'No CUDA device found!'

        self.model = nn.DataParallel(self.model, multi_gpu_device_ids, multi_gpu_output_device, multi_gpu_dim)
        temp_device = self._device
        if multi_gpu_output_device is not None:
            self.to(multi_gpu_output_device)
        else:
            self.to(torch.device('cuda'))

        self.train(loss_wrapper,
                   optimizer,
                   train_data_loader,
                   evaluation_data_loaders,
                   batch_input_key,
                   evaluators,
                   callbacks,
                   gradient_accumulation_steps)

        self.model = self.model.module
        self.to(temp_device)

    def train(self,
              loss_wrapper,
              optimizer,
              train_data_loader,
              evaluation_data_loaders=None,
              batch_input_key='input',
              evaluators=None,
              callbacks=None,
              gradient_accumulation_steps=1):
        """
        Trains the model on a dataset.

        :param loss_wrapper: Object derived from AbstractLossWrapper that wraps the calculation of the loss.
        :param optimizer: Optimizer object.
        :param train_data_loader: DataLoader object that generates batches of the train dataset. Each batch must be a
            Dict that contains at least a Tensor or a list/tuple of Tensors containing the input(s) of the model
            (key=`batch_input_key`) as well as all the information needed by the loss_wrapper.
        :param evaluation_data_loaders: Dictionary containing the evaluation data-loaders. The keys are the datasets'
            names. Each batch generated by the dataloaders must be a  Dict that contains the input of the model
            (key=`batch_input_key`) as well as the information needed by the evaluators.
        :param batch_input_key: Key of the Dicts returned by the Dataloader objects that corresponds to the input of the
            model.
        :param evaluators: Dictionary containing objects derived from AbstractEvaluator. The keys are the evaluators'
            names.
        :param callbacks: List containing TrainingCallback objects. They are used in order to inject functionality at
            several points of the training process. Default is NumberOfEpochsStoppingCriterionCallback(10) that stops
            training after the 10th iteration (counting from 0).
        :param gradient_accumulation_steps: Number of backward calls before an optimization step. Used in order to
            simulate a larger batch size).
        :return: List containing the results for each epoch.
        """

        trainer = _Trainer(self,
                           loss_wrapper,
                           optimizer,
                           train_data_loader,
                           evaluation_data_loaders,
                           batch_input_key,
                           evaluators,
                           callbacks,
                           gradient_accumulation_steps)

        return trainer.run()

    def predict(self,
                data_loader,
                perform_last_activation=True,
                batch_id_key=None,
                batch_input_key='input',
                model_output_key=None):
        """
        Computes the outputs of the model on a dataset.

        :param data_loader: DataLoader object that generates batches of data. Each batch must be a Dict that contains at
            least a Tensor or a list/tuple of Tensors containing the input(s) of the model(key=`batch_input_key`).
        :param perform_last_activation: Whether to perform the last_activation.
        :param batch_id_key: Key where the dict returned by the dataloader contains the ids of the examples. Leave None
            if there are no ids.
        :param batch_input_key: Key where the dict returned by the dataloader contains the input of the model.
        :param model_output_key: Key where the dict returned by the model contains the actual predictions. Leave None
            if the model returns only the predictions.
        :return: Dict containing a list of predictions (key=`outputs`) and a list of ids (key=`batch_id_key`) if
            provided by the dataloader.
        """

        output_list = []
        ids_list = []

        with torch.no_grad():
            for i, batch in enumerate(auto_tqdm(data_loader, ncols=NCOLS)):

                if batch_id_key is not None:
                    if type(batch[batch_id_key]) is torch.Tensor:
                        ids_list.extend(batch[batch_id_key].tolist())
                    else:
                        ids_list.extend(batch[batch_id_key])

                outputs = self.predict_batch(batch[batch_input_key])

                if model_output_key is not None:
                    outputs = outputs[model_output_key]

                if self.last_activation is not None and perform_last_activation:
                    outputs = self.last_activation(outputs)

                output_list.extend(outputs.tolist())

        if batch_id_key is not None:
            return {batch_id_key: ids_list, 'outputs': output_list}
        else:
            return {'outputs': output_list}

    def pure_predict(self, data_loader, batch_input_key='input', keep_batches=True):
        """
        Computes the output of the model on a dataset.

        :param data_loader: DataLoader object that generates batches of data. Each batch must be a Dict that contains at
            least a Tensor or a list/tuple of Tensors containing the input(s) of the model(key=`batch_input_key`).
        :param batch_input_key: The key of the batches returned by the data_loader that contains the input of the
            model.
        :param keep_batches: If set to True then the method also returns a list of the batches returned by the
            dataloader.
        :return: Dict containing a list of batched model outputs (key=`output_list`) and a list of batches as returned
            by the dataloader (key=`batch_list`) if keep_batches is set to True.
        """

        batch_list = []
        output_list = []

        with torch.no_grad():
            for i, batch in enumerate(auto_tqdm(data_loader, ncols=NCOLS)):
                if keep_batches:
                    batch_list.append(batch)
                output = self.predict_batch(batch[batch_input_key])
                output = self._pure_predict_convert_output(output)
                output_list.append(output)

        if keep_batches:
            return {'batch_list': batch_list, 'output_list': output_list}
        else:
            return {'output_list': output_list}

    def _pure_predict_convert_output(self, output):

        if type(output) is dict:
            converted_output = {}
            for k in output:
                converted_output[k] = self._pure_predict_convert_output(output[k])
        else:
            converted_output = output.detach().cpu()

        return converted_output

    def predict_batch(self, single_batch_input):
        """
        Computes the output of the model for a single batch.

        :param single_batch_input: Tensor or list of Tensors [tensor_1, tensor_2, ...] that correspond to the input of
            the model.
        :return: The output of the model.
        """

        batch_inputs = []

        if type(single_batch_input) is not list and type(single_batch_input) is not tuple:
            single_batch_input = [single_batch_input]

        for batch_input in single_batch_input:
            batch_input = batch_input.to(self._device)
            batch_inputs.append(batch_input)

        return self.model(*batch_inputs)

    def evaluate(self, data_loader, evaluators, batch_input_key='input'):
        """
        Evaluates the model on a dataset.

        :param data_loader: DataLoader object that generates batches of the evaluation dataset. Each batch must be a
            Dict that contains the input of the model (key=`batch_input_key`) as well as the information needed by
            the evaluators.
        :param evaluators: Dictionary containing objects derived from AbstractEvaluator. The keys are the evaluators'
            names.
        :param batch_input_key: The key of the batches returned by the data_loader that contains the input of the
            model.
        :return: Dict containing an object derived from AbstractEvaluatorResults for each evaluator.
        """

        self.model.train(False)

        for evaluator_name in evaluators:
            evaluators[evaluator_name].reset()

        with torch.no_grad():
            for i, batch in enumerate(auto_tqdm(data_loader, ncols=NCOLS)):

                outputs = self.predict_batch(batch[batch_input_key])

                for evaluator_name in evaluators:
                    evaluators[evaluator_name].step(outputs, batch, self.last_activation)

        results = {}
        for evaluator_name in evaluators:
            results[evaluator_name] = evaluators[evaluator_name].calculate()

        return results

    def to(self, device):
        """
        Transfers the model to the specified device.

        :param device: Device to be transferred to.
        :return: Returns the model after moving it to the device (inplace).
        """

        self.model = self.model.to(device)
        self._device = device

        return self

    def save(self, f):
        """
        Saves the System to a file.

        :param f: a file-like object (has to implement write and flush) or a string containing a file name.
        """

        torch.save({
            'model': self.model,
            'last_activation': self.last_activation
        }, f)

    @staticmethod
    def load(f):
        """
        Loads a System from a file. The model will reside in the CPU initially.

        :param f: a file-like object (has to implement write and flush) or a string containing a file name.
        """

        loaded_data = torch.load(f, map_location=torch.device('cpu'))
        return System(loaded_data['model'], loaded_data['last_activation'])

    def save_model_state(self, f):
        """
        Saves the model's state to a file.

        :param f: a file-like object (has to implement write and flush) or a string containing a file name.
        """

        if isinstance(self.model, nn.DataParallel):
            model_state = {k[len('module.'):]: v for k, v in self.model.state_dict().items()}
        else:
            model_state = self.model.state_dict()

        torch.save(model_state, f)

    def load_model_state(self, f, strict=True):
        """
        Loads the model's state from a file.

        :param f: a file-like object (has to implement write and flush) or a string containing a file name.
        :param strict: Whether the file must contain exactly the same weight keys as the model.
        :return: NamedTuple with two lists (`missing_keys` and `unexpected_keys`).
        """

        model_state = torch.load(f, map_location=torch.device('cpu'))
        if isinstance(self.model, nn.DataParallel):
            model_state = {'module.' + k: v for k, v in model_state.items()}

        invalid_keys = self.model.load_state_dict(model_state, strict)
        self.model.to(self._device)
        return invalid_keys


class _Trainer(object):

    def __init__(self,
                 system,
                 loss_wrapper,
                 optimizer,
                 train_data_loader,
                 evaluation_data_loaders,
                 batch_input_key,
                 evaluators,
                 callbacks,
                 gradient_accumulation_steps):
        """
        Used internally to train the model on a dataset.

        :param system: The system object.
        :param loss_wrapper: Object derived from AbstractLossWrapper that wraps the calculation of the loss.
        :param optimizer: Optimizer object.
        :param train_data_loader: DataLoader object that generates batches of the train dataset. Each batch must be a
            Dict that contains at least a Tensor or a list/tuple of Tensors containing the input(s) of the model
            (key=`batch_input_key`) as well as all the information needed by the `loss_wrapper`.
        :param evaluation_data_loaders: Dictionary containing the evaluation data-loaders. The keys are the datasets'
            names. Each batch generated by the dataloaders must be a  Dict that contains the input of the model
            (key=`batch_input_key`) as well as the information needed by the `evalurators`.
        :param batch_input_key: Key of the Dicts returned by the Dataloader objects that corresponds to the input of the
            model.
        :param evaluators: Dictionary containing objects derived from AbstractEvaluator. The keys are the evaluators'
            names.
        :param callbacks: List containing TrainingCallback objects. They are used in order to inject functionality at
            several points of the training process. Default is NumberOfEpochsStoppingCriterionCallback(10) that stops
            training after the 10th iteration (counting from 0).
        :param gradient_accumulation_steps: Number of backward calls before an optimization step. Used in order to
            simulate a larger batch size).
        :return: List containing the results for each epoch.
        """

        self.training_context = {

            'system': system,
            # list of all results
            '_results_history': [],
            # loss_wrapper
            'loss_wrapper': loss_wrapper,
            # optimizer
            'optimizer': optimizer,
            # stop training
            'stop_training': False,
            # current_epoch
            '_current_epoch': -1,
            # current_batch
            'current_batch': None,
            # current output
            'current_output': None,
            # current loss
            'current_loss': None

        }

        self.train_data_loader = train_data_loader
        self.evaluation_data_loaders = evaluation_data_loaders
        self.batch_input_key = batch_input_key
        self.evaluators = evaluators
        self.callbacks = callbacks
        self.gradient_accumulation_steps = gradient_accumulation_steps

        if self.callbacks is None:
            self.callbacks = [NumberOfEpochsStoppingCriterionCallback(1)]
        elif not any([issubclass(type(cb), StoppingCriterionCallback) for cb in self.callbacks]):
            self.callbacks.append(NumberOfEpochsStoppingCriterionCallback(1))

    def run(self):

        for callback in self.callbacks:
            callback.on_training_start(self.training_context)

        # Train the Model
        while not self.training_context['stop_training']:
            self._train_epoch()
            self._train_evaluation()
            auto_tqdm.write('')

        for callback in self.callbacks:
            callback.on_training_end(self.training_context)

        self.training_context['system'].model.train(False)

        return self.training_context['_results_history']

    def _train_epoch(self):
        """
        Trains the model for a single epoch.
        """

        self.training_context['_current_epoch'] += 1

        self.training_context['system'].model.train(True)

        for callback in self.callbacks:
            callback.on_epoch_start(self.training_context)

        pre_time = time.time()
        auto_tqdm.write('-' * 80)
        auto_tqdm.write('')
        auto_tqdm.write('Epoch: %d' % (self.training_context['_current_epoch']))
        auto_tqdm.write('')
        auto_tqdm.write('Training...')
        auto_tqdm.write('')

        pbar = auto_tqdm(total=len(self.train_data_loader), ncols=NCOLS)

        cum_loss = 0
        self.training_context['optimizer'].zero_grad()

        for i, batch in enumerate(self.train_data_loader):
            perform_opt_step = (i % self.gradient_accumulation_steps == 0) or (i == (len(self.train_data_loader) - 1))
            cum_loss += self._train_batch(batch, perform_opt_step)

            train_loss = cum_loss / (i + 1)
            pbar.update(1)
            pbar.set_postfix(ordered_dict=OrderedDict([('loss', '%5.4f' % train_loss)]))

        for callback in self.callbacks:
            callback.on_epoch_end(self.training_context)

        pbar.close()
        auto_tqdm.write('Time elapsed: %d' % (time.time() - pre_time))
        auto_tqdm.write('')

    def _train_batch(self, batch, perform_opt_step):
        """
        Trains the model on a batch.

        :param batch: Batch as returned by a Dataloader.
        :param perform_opt_step: Whether to perform an optimization step.
        :return: The loss for this batch.
        """

        self.training_context['current_batch'] = batch

        for callback in self.callbacks:
            callback.on_batch_start(self.training_context)

        self.training_context['current_output'] = self.training_context['system'].predict_batch(
            batch[self.batch_input_key])

        self.training_context['current_batch'] = None

        for callback in self.callbacks:
            callback.post_predict(self.training_context)

        self.training_context['current_loss'] = self.training_context['loss_wrapper'].calculate_loss(
            self.training_context['current_output'],
            batch,
            self.training_context,
            self.training_context['system'].last_activation)

        self.training_context['current_output'] = None

        for callback in self.callbacks:
            callback.post_loss_calculation(self.training_context)

        # Forward + Backward + Optimize
        self.training_context['current_loss'].backward()

        for callback in self.callbacks:
            callback.post_backward_calculation(self.training_context)

        if perform_opt_step:
            for callback in self.callbacks:
                callback.pre_optimization_step(self.training_context)
            self.training_context['optimizer'].step()
            self.training_context['optimizer'].zero_grad()

        cur_batch_loss = self.training_context['current_loss'].item()

        for callback in self.callbacks:
            callback.on_batch_end(self.training_context)

        self.training_context['current_loss'] = None

        return cur_batch_loss

    def _train_evaluation(self):
        """
        Evaluates the model after each epoch.
        """

        if self.evaluation_data_loaders is not None and self.evaluators is not None:

            auto_tqdm.write('Evaluating...')
            auto_tqdm.write('')

            for callback in self.callbacks:
                callback.on_evaluation_start(self.training_context)

            current_results = {}
            for current_dataset_name in self.evaluation_data_loaders:
                auto_tqdm.write(current_dataset_name)
                current_dataset_results = self.training_context['system'].evaluate(
                    self.evaluation_data_loaders[current_dataset_name],
                    self.evaluators,
                    self.batch_input_key)
                current_results[current_dataset_name] = current_dataset_results
                for evaluator_name in self.evaluators:
                    auto_tqdm.write(str(current_results[current_dataset_name][evaluator_name]))

            self.training_context['_results_history'].append(current_results)

            for callback in self.callbacks:
                callback.on_evaluation_end(self.training_context)
