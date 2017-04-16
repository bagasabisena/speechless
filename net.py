from functools import reduce
from pathlib import Path

import editdistance
from keras import backend
from keras.callbacks import Callback, TensorBoard
from keras.engine import Input, Layer, Model
from keras.layers import Lambda, Dropout, Conv1D
from keras.models import Sequential
from keras.optimizers import Optimizer, Adam
from lazy import lazy
from numpy import ndarray, zeros, array, reshape, insert, random, concatenate
from typing import List, Callable, Iterable, Tuple, Dict, Optional

from grapheme_enconding import CtcGraphemeEncoding, english_frequent_characters, AsgGraphemeEncoding
from labeled_example import LabeledSpectrogram
from tools import average, mkdir, single, read_text


class ExpectationVsPrediction:
    def __init__(self, expected: str, predicted: str):
        self.expected = expected
        self.predicted = predicted
        self.expected_letter_count = len(self.expected)
        self.expected_words = self.expected.split()
        self.expected_word_count = len(self.expected_words)

    @lazy
    def letter_error_count(self) -> float:
        return editdistance.eval(self.expected, self.predicted)

    @lazy
    def word_error_count(self) -> float:
        return editdistance.eval(self.expected_words, self.predicted.split())

    @lazy
    def letter_error_rate(self) -> float:
        return self.letter_error_count / self.expected_letter_count

    @lazy
    def word_error_rate(self) -> float:
        return self.word_error_count / self.expected_word_count

    def __str__(self):
        return 'Expected:  "{}"\nPredicted: "{}"\nErrors: {} letters ({}%), {} words ({}%).'.format(
            self.expected, self.predicted,
            self.letter_error_count, round(self.letter_error_rate * 100),
            self.word_error_count, round(self.word_error_rate * 100))


class ExpectationsVsPredictions:
    def __init__(self, results: List[ExpectationVsPrediction]):
        self.results = results

    @lazy
    def average_letter_error_count(self):
        return average([r.letter_error_count for r in self.results])

    @lazy
    def average_word_error_count(self):
        return average([r.word_error_count for r in self.results])

    @lazy
    def average_letter_error_rate(self) -> float:
        return average([r.letter_error_rate for r in self.results])

    @lazy
    def average_word_error_rate(self) -> float:
        return average([r.word_error_rate for r in self.results])

    def __str__(self):
        return "\n\n".join(str(r) for r in self.results) + "\n\n" + self.summary_line()

    def summary_line(self):
        return "Errors on average: {:.1f} letters ({:.2f}%), {:.1f} words ({:.2f}%).".format(
            self.average_letter_error_count, self.average_letter_error_rate * 100,
            self.average_word_error_count, self.average_word_error_rate * 100)


class Wav2Letter:
    """Speech-recognition network based on wav2letter (https://arxiv.org/pdf/1609.03193v2.pdf)."""

    class InputNames:
        input_batch = "input_batch"
        label_batch = "label_batch"
        prediction_lengths = "prediction_lenghts"
        label_lengths = "label_lenghts"

    def __init__(self,
                 input_size_per_time_step: int,
                 allowed_characters: List[chr] = english_frequent_characters,
                 use_raw_wave_input: bool = False,
                 activation: str = "relu",
                 output_activation: str = "softmax",
                 optimizer: Optimizer = Adam(1e-4),
                 dropout: Optional[float] = None,
                 load_model_from_directory: Optional[Path] = None,
                 load_epoch: Optional[int] = None,
                 allowed_characters_for_loaded_model: Optional[List[chr]] = None,
                 frozen_layer_count: int = 0,
                 use_asg: bool = False,
                 asg_transition_probabilities: Optional[ndarray] = None,
                 asg_initial_probabilities: Optional[ndarray] = None,
                 kenlm_directory: Path = None):

        self.kenlm_directory = kenlm_directory
        self.allowed_characters_for_loaded_model = allowed_characters_for_loaded_model
        self.grapheme_encoding = AsgGraphemeEncoding(allowed_characters=allowed_characters) \
            if use_asg else CtcGraphemeEncoding(allowed_characters=allowed_characters)

        self.asg_transition_probabilities = self._default_asg_transition_probabilities(
            self.grapheme_encoding.grapheme_set_size) \
            if asg_transition_probabilities is None else asg_transition_probabilities

        self.asg_initial_probabilities = self._default_asg_initial_probabilities(
            self.grapheme_encoding.grapheme_set_size) \
            if asg_initial_probabilities is None else asg_initial_probabilities

        self.use_asg = use_asg
        self.frozen_layer_count = frozen_layer_count
        self.output_activation = output_activation
        self.activation = activation
        self.use_raw_wave_input = use_raw_wave_input
        self.input_size_per_time_step = input_size_per_time_step
        self.optimizer = optimizer
        self.load_epoch = load_epoch
        self.dropout = dropout
        self.predictive_net = self.create_predictive_net()
        self.prediction_phase_flag = 0.

        if load_model_from_directory is not None:
            if self.allowed_characters_for_loaded_model is None:
                self.predictive_net.load_weights(str(load_model_from_directory / self.model_file_name(load_epoch)))
            else:
                loaded_allowed_character_count = len(allowed_characters_for_loaded_model)
                extra_allowed_character_count = len(allowed_characters) - loaded_allowed_character_count

                if extra_allowed_character_count < 0 or \
                                allowed_characters[
                                :loaded_allowed_character_count] != allowed_characters_for_loaded_model:
                    raise ValueError(
                        "Allowed characters must begin with the allowed characters for loaded model in the same order.")

                original_wav2letter = Wav2Letter(input_size_per_time_step=input_size_per_time_step,
                                                 allowed_characters=allowed_characters_for_loaded_model,
                                                 use_raw_wave_input=use_raw_wave_input,
                                                 activation=activation,
                                                 output_activation=output_activation,
                                                 optimizer=optimizer,
                                                 dropout=dropout,
                                                 load_model_from_directory=load_model_from_directory,
                                                 load_epoch=load_epoch,
                                                 frozen_layer_count=frozen_layer_count,
                                                 use_asg=use_asg,
                                                 asg_initial_probabilities=asg_initial_probabilities,
                                                 asg_transition_probabilities=asg_transition_probabilities)

                for index, layer in enumerate(self.predictive_net.layers):
                    original_weights, original_biases = original_wav2letter.predictive_net.layers[index].get_weights()

                    if index == len(self.predictive_net.layers) - 1:
                        original_shape = original_weights.shape
                        grapheme_axis = 2
                        to_insert = zeros((original_shape[0], original_shape[1], extra_allowed_character_count))
                        original_weights = insert(
                            original_weights,
                            axis=grapheme_axis,
                            obj=[len(self.allowed_characters_for_loaded_model)],
                            values=to_insert)

                        original_biases = insert(
                            original_biases, original_biases.shape[0] - 1,
                            zeros((extra_allowed_character_count,)), axis=0)

                    layer.set_weights([original_weights, original_biases])

    @staticmethod
    def _default_asg_transition_probabilities(grapheme_set_size: int) -> ndarray:
        asg_transition_probabilities = random.randint(1, 15,
                                                      (grapheme_set_size + 1, grapheme_set_size + 1))
        zero_array = zeros(grapheme_set_size + 1)
        asg_transition_probabilities[0] = zero_array
        asg_transition_probabilities[:, 0] = zero_array
        # sum up each column, add dummy 1 in front for easier division later
        transition_norms = concatenate(([1], asg_transition_probabilities[:, 1:].sum(axis=0)))
        asg_transition_probabilities = asg_transition_probabilities / transition_norms
        return asg_transition_probabilities

    @staticmethod
    def _default_asg_initial_probabilities(grapheme_set_size: int) -> ndarray:
        asg_initial_probabilities = random.randint(1, 15, grapheme_set_size + 1)
        asg_initial_probabilities[0] = 0
        asg_initial_probabilities = asg_initial_probabilities / asg_initial_probabilities.sum()
        # N.B. beware that initial_logprobs[0] is now -inf, NOT 0!
        return asg_initial_probabilities

    def create_predictive_net(self) -> Sequential:
        """Returns the part of the net that predicts grapheme probabilities given a spectrogram.
         A loss operation is not contained.
         As described here: https://arxiv.org/pdf/1609.03193v2.pdf
        """

        def convolution(name: str, filter_count: int, filter_length: int, strides: int = 1,
                        activation: str = self.activation,
                        input_dim: int = None,
                        never_dropout: bool = False) -> List[Layer]:
            return ([] if self.dropout is None or never_dropout else [
                Dropout(self.dropout, input_shape=(None, input_dim),
                        name="dropout_before_{}".format(name))]) + [
                       Conv1D(filters=filter_count, kernel_size=filter_length, strides=strides,
                              activation=activation, name=name, input_shape=(None, input_dim), padding="same")]

        main_filter_count = 250

        def input_convolutions() -> List[Conv1D]:
            raw_wave_convolution_if_needed = convolution(
                "wave_conv", filter_count=main_filter_count, filter_length=250, strides=160,
                input_dim=self.input_size_per_time_step) if self.use_raw_wave_input else []

            return raw_wave_convolution_if_needed + convolution(
                "striding_conv", filter_count=main_filter_count, filter_length=48, strides=2,
                input_dim=None if self.use_raw_wave_input else self.input_size_per_time_step)

        def inner_convolutions() -> List[Conv1D]:
            return [layer for i in
                    range(1, 8) for layer in
                    convolution("inner_conv_{}".format(i), filter_count=main_filter_count, filter_length=7)]

        def output_convolutions() -> List[Conv1D]:
            out_filter_count = 2000
            return [layer for conv in [
                convolution("big_conv_1", filter_count=out_filter_count, filter_length=32, never_dropout=True),
                convolution("big_conv_2", filter_count=out_filter_count, filter_length=1, never_dropout=True),
                convolution("output_conv", filter_count=self.grapheme_encoding.grapheme_set_size,
                            filter_length=1,
                            activation=self.output_activation, never_dropout=True)
            ] for layer in conv]

        layers = input_convolutions() + inner_convolutions() + output_convolutions()

        if self.frozen_layer_count > 0:
            print("All but {} layers frozen.".format(len(layers) - self.frozen_layer_count))

        for layer in layers[:self.frozen_layer_count]:
            layer.trainable = False

        return Sequential(layers)

    @lazy
    def input_to_prediction_length_ratio(self):
        """Returns which factor shorter the output is compared to the input caused by striding."""
        return reduce(lambda x, y: x * y,
                      [layer.strides[0] for layer in self.predictive_net.layers if
                       isinstance(layer, Conv1D)], 1)

    def prediction_batch(self, input_batch: ndarray) -> ndarray:
        """Predicts a grapheme probability batch given a spectrogram batch, employing the learned predictive network."""
        # Indicates to use prediction phase in order to disable dropout (see backend.learning_phase documentation):
        return backend.function(self.predictive_net.inputs + [backend.learning_phase()], self.predictive_net.outputs)(
            [input_batch, self.prediction_phase_flag])[0]

    @lazy
    def loss_net(self) -> Model:
        """Returns the network that yields a loss given both input spectrograms and labels. Used for training."""
        input_batch = self._input_batch_input()
        label_batch = Input(name=Wav2Letter.InputNames.label_batch, shape=(None,), dtype='int32')
        prediction_lengths = self._prediction_lengths_input()
        label_lengths = Input(name=Wav2Letter.InputNames.label_lengths, shape=(1,), dtype='int64')

        asg_transition_probabilities_variable = backend.variable(value=self.asg_transition_probabilities,
                                                                 name="asg_transition_probabilities")
        asg_initial_probabilities_variable = backend.variable(value=self.asg_initial_probabilities,
                                                              name="asg_initial_probabilities")
        # Since Keras doesn't currently support loss functions with extra parameters,
        # we define a custom lambda layer yielding one single real-valued CTC loss given the grapheme probabilities:
        loss_layer = Lambda(Wav2Letter._asg_lambda if self.use_asg else Wav2Letter._ctc_lambda,
                            name='asg_loss' if self.use_asg else 'ctc_loss',
                            output_shape=(1,),
                            arguments={"transition_probabilities": asg_transition_probabilities_variable,
                                       "initial_probabilities": asg_initial_probabilities_variable} if self.use_asg else None)

        # ([asg_transition_probabilities_variable, asg_initial_probabilities_variable] if self.use_asg else [])

        # This loss layer is placed atop the predictive network and provided with additional arguments,
        # namely the label batch and prediction/label sequence lengths:
        loss = loss_layer(
            [self.predictive_net(input_batch), label_batch, prediction_lengths, label_lengths])

        loss_net = Model(inputs=[input_batch, label_batch, prediction_lengths, label_lengths], outputs=[loss])
        # Since loss is already calculated in the last layer of the net, we just pass through the results here.
        # The loss dummy labels have to be given to satify the Keras API.
        loss_net.compile(loss=lambda dummy_labels, ctc_loss: ctc_loss, optimizer=self.optimizer)
        return loss_net

    def _prediction_lengths_input(self):
        return Input(name=Wav2Letter.InputNames.prediction_lengths, shape=(1,), dtype='int64')

    @staticmethod
    def _asg_lambda(args, transition_probabilities=None, initial_probabilities=None):
        # keras implementation can be plugged in here once ready:
        raise NotImplementedError("ASG is not yet implemented.")

    # No type hints here because the annotations cause an error in the Keras library:
    @staticmethod
    def _ctc_lambda(args):
        prediction_batch, label_batch, prediction_lengths, label_lengths = args
        return backend.ctc_batch_cost(y_true=label_batch, y_pred=prediction_batch,
                                      input_length=prediction_lengths, label_length=label_lengths)

    @lazy
    def kenlm_net(self):
        input_batch = self._input_batch_input()
        prediction_lengths = self._prediction_lengths_input()

        decoding_layer = Lambda(self._kenlm_decode_lambda, name='kenlm_decode')

        prediction_batch = self.predictive_net(input_batch)
        decoded = decoding_layer([prediction_batch, prediction_lengths])

        return Model(inputs=[input_batch, prediction_lengths], outputs=[decoded])

    def _kenlm_decode_lambda(self, args):
        import tensorflow as tf

        """
        Modified version of ctc_decode employing a kenlm decoder. 
        Needs a modified version of tensorflow available at https://github.com/timediv/tensorflow-with-kenlm to run.
            # Returns
                Tuple:
                    List: Most probable decoded sequence (as a single element of a list). 
                    Important: blank labels are returned as `-1`. 
                    Tensor `(1, )` that contains the log probability of each decoded sequence.
        """

        prediction_batch, prediction_lengths = args
        allowed_characters = self.grapheme_encoding.allowed_characters
        expected_characters = list(
            single(read_text(self.kenlm_directory / "vocabulary", encoding='utf8').splitlines()).lower())
        if allowed_characters != expected_characters:
            raise ValueError("Allowed characters {} differ from those expected by kenlm decoder: {}".
                             format(allowed_characters, expected_characters))

        y_pred = tf.log(tf.transpose(prediction_batch, perm=[1, 0, 2]) + 1e-8)
        input_length = tf.to_int32(tf.squeeze(prediction_lengths, axis=[1]))

        (decoded, log_prob) = tf.nn.ctc_beam_search_decoder(inputs=y_pred,
                                                            sequence_length=input_length,
                                                            kenlm_directory_path=str(self.kenlm_directory))

        decoded_dense = single([tf.sparse_to_dense(st.indices, st.dense_shape, st.values, default_value=-1)
                                for st in decoded])
        return decoded_dense

    def predict_with_kenlm(self, spectrograms: List[ndarray]) -> List[str]:
        input_batch, prediction_lengths = self._input_batch_and_prediction_lengths(spectrograms)

        decoded = single(backend.function(self.kenlm_net.inputs + [backend.learning_phase()], self.kenlm_net.outputs)(
            [input_batch, self._prediction_length_batch(prediction_lengths, batch_size=len(spectrograms)),
             self.prediction_phase_flag]))

        decoded[decoded < 0] = self.grapheme_encoding.ctc_blank

        return self.grapheme_encoding.decode_grapheme_batch(decoded, prediction_lengths)

    def _input_batch_input(self):
        return Input(name=Wav2Letter.InputNames.input_batch, batch_shape=self.predictive_net.input_shape)

    def predict(self, spectrograms: List[ndarray]) -> List[str]:
        return self.predict_with_kenlm(spectrograms) if self.kenlm_directory else self.predict_greedily(spectrograms)

    def predict_greedily(self, spectrograms: List[ndarray]) -> List[str]:
        input_batch, prediction_lengths = self._input_batch_and_prediction_lengths(spectrograms)

        return self.grapheme_encoding.decode_prediction_batch(self.prediction_batch(input_batch),
                                                              prediction_lengths=prediction_lengths)

    def predict_single(self, spectrogram: ndarray) -> str:
        return self.predict([spectrogram])[0]

    def loss(self, labeled_spectrogram_batches: Iterable[List[LabeledSpectrogram]]):
        inputs = self._generator(labeled_spectrogram_batches)

        def print_and_get_batch_loss(x, y):
            loss = self.loss_net.evaluate(x, y, batch_size=y.shape[0])

            print("Batch loss: " + str(loss))

            return loss

        return average([print_and_get_batch_loss(x, y) for x, y in inputs])

    def _generator(self, labeled_spectrogram_batches: Iterable[List[LabeledSpectrogram]]) -> Iterable[
        Tuple[Dict, ndarray]]:
        for index, labeled_spectrogram_batch in enumerate(labeled_spectrogram_batches):
            batch_size = len(labeled_spectrogram_batch)
            dummy_labels_for_dummy_loss_function = zeros((batch_size,))
            training_input_dictionary = self._training_input_dictionary(
                labeled_spectrogram_batch=labeled_spectrogram_batch)
            yield (training_input_dictionary, dummy_labels_for_dummy_loss_function)

    def expectations_vs_predictions(
            self, labeled_spectrogram_batch: List[LabeledSpectrogram]) -> ExpectationsVsPredictions:
        return ExpectationsVsPredictions([ExpectationVsPrediction(expected, predicted) for expected, predicted in zip(
            (s.label for s in labeled_spectrogram_batch),
            self.predict([s.z_normalized_transposed_spectrogram() for s in labeled_spectrogram_batch]))])

    def train(self,
              labeled_spectrogram_batches: Iterable[List[LabeledSpectrogram]],
              preview_labeled_spectrogram_batch: List[LabeledSpectrogram],
              tensor_board_log_directory: Path,
              net_directory: Path,
              batches_per_epoch: int):
        self.loss_net.fit_generator(self._generator(labeled_spectrogram_batches), epochs=100000000,
                                    steps_per_epoch=batches_per_epoch,
                                    callbacks=self.create_callbacks(
                                        callback=lambda: print(self.expectations_vs_predictions(
                                            preview_labeled_spectrogram_batch)),
                                        tensor_board_log_directory=tensor_board_log_directory,
                                        net_directory=net_directory),
                                    initial_epoch=self.load_epoch if (self.load_epoch is not None) else 0)

    @staticmethod
    def model_file_name(epoch: int) -> str:
        return "weights-epoch{}.h5".format(epoch)

    def create_callbacks(self, callback: Callable[[], None], tensor_board_log_directory: Path, net_directory: Path,
                         callback_step: int = 1, save_step: int = 1) -> List[Callback]:
        class CustomCallback(Callback):
            def on_epoch_end(self_callback, epoch, logs=()):
                if epoch % callback_step == 0:
                    callback()

                if epoch % save_step == 0 and epoch > 0:
                    mkdir(net_directory)

                    self.predictive_net.save_weights(str(net_directory / self.model_file_name(epoch)))

        tensorboard_if_running_tensorboard = [TensorBoard(log_dir=str(tensor_board_log_directory),
                                                          write_images=True)] if backend.backend() == 'tensorflow' else []
        return tensorboard_if_running_tensorboard + [CustomCallback()]

    def _input_batch_and_prediction_lengths(self, spectrograms: List[ndarray]) -> Tuple[ndarray, List[int]]:
        batch_size = len(spectrograms)
        input_size_per_time_step = spectrograms[0].shape[1]
        input_lengths = [spectrogram.shape[0] for spectrogram in spectrograms]
        prediction_lengths = [s // self.input_to_prediction_length_ratio for s in input_lengths]
        input_batch = zeros((batch_size, max(input_lengths), input_size_per_time_step))
        for index, spectrogram in enumerate(spectrograms):
            input_batch[index, :spectrogram.shape[0], :spectrogram.shape[1]] = spectrogram

        return input_batch, prediction_lengths

    def _prediction_length_batch(self, prediction_lengths: List[int], batch_size: int) -> ndarray:
        return reshape(array(prediction_lengths), (batch_size, 1))

    def _training_input_dictionary(self, labeled_spectrogram_batch: List[LabeledSpectrogram]) -> Dict[str, ndarray]:
        spectrograms = [x.z_normalized_transposed_spectrogram() for x in labeled_spectrogram_batch]
        labels = [x.label for x in labeled_spectrogram_batch]
        input_batch, prediction_lengths = self._input_batch_and_prediction_lengths(spectrograms)

        # Sets learning phase to training to enable dropout (see backend.learning_phase documentation for more info):
        training_phase_flag_tensor = array([True])
        return {
            Wav2Letter.InputNames.input_batch: input_batch,
            Wav2Letter.InputNames.prediction_lengths: self._prediction_length_batch(prediction_lengths,
                                                                                    batch_size=len(spectrograms)),
            Wav2Letter.InputNames.label_batch: self.grapheme_encoding.encode_label_batch(labels),
            Wav2Letter.InputNames.label_lengths: reshape(array([len(label) for label in labels]),
                                                         (len(labeled_spectrogram_batch), 1)),
            'keras_learning_phase': training_phase_flag_tensor
        }
