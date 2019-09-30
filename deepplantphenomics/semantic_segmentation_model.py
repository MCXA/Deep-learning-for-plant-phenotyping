from . import layers, definitions, DPPModel
import numpy as np
import tensorflow as tf
import os
import warnings
import copy
from tqdm import tqdm


class SemanticSegmentationModel(DPPModel):
    _problem_type = definitions.ProblemType.SEMANTIC_SEGMETNATION
    _loss_fn = 'sigmoid cross entropy'
    _supported_loss_fns = ['sigmoid cross entropy']
    _supported_augmentations = [definitions.AugmentationType.CONTRAST_BRIGHT]

    def __init__(self, debug=False, load_from_saved=False, save_checkpoints=True, initialize=True, tensorboard_dir=None,
                 report_rate=100, save_dir=None):
        super().__init__(debug, load_from_saved, save_checkpoints, initialize, tensorboard_dir, report_rate, save_dir)

        # State variables specific to semantic segmentation for constructing the graph and passing to Tensorboard
        self._graph_forward_pass = None

    def _graph_tensorboard_summary(self, l2_cost, gradients, variables, global_grad_norm):
        super()._graph_tensorboard_common_summary(l2_cost, gradients, variables, global_grad_norm)

        # Summaries specific to semantic segmentation
        # We send in the last layer's output size (i.e. the final image dimensions) to get_weights_as_image
        # because xx and x_test_predicted have dynamic dims [?,?,?,?], so we need actual numbers passed in
        train_images_summary = self._get_weights_as_image(
            tf.transpose(self._graph_forward_pass, (1, 2, 3, 0)), self._layers[-1].output_size)
        tf.summary.image('masks/train', train_images_summary, collections=['custom_summaries'])
        if self._validation:
            tf.summary.scalar('validation/loss', self._graph_ops['val_cost'],
                              collections=['custom_summaries'])
            val_images_summary = self._get_weights_as_image(
                tf.transpose(self._graph_ops['x_val_predicted'], (1, 2, 3, 0)), self._layers[-1].output_size)
            tf.summary.image('masks/validation', val_images_summary, collections=['custom_summaries'])

        self._graph_ops['merged'] = tf.summary.merge_all(key='custom_summaries')

    def _assemble_graph(self):
        with self._graph.as_default():
            self._log('Assembling graph...')

            self._log('Graph: Parsing dataset...')
            with tf.device('/device:cpu:0'):  # Only do preprocessing on the CPU to limit data transfer between devices
                self._graph_parse_data()

                # Batch the datasets and create iterators for them
                train_iter = self._batch_and_iterate(self._train_dataset, shuffle=True)
                if self._testing:
                    test_iter = self._batch_and_iterate(self._test_dataset)
                if self._validation:
                    val_iter = self._batch_and_iterate(self._val_dataset)

                if self._has_moderation:
                    train_mod_iter = self._batch_and_iterate(self._train_moderation_features)
                    if self._testing:
                        test_mod_iter = self._batch_and_iterate(self._test_moderation_features)
                    if self._validation:
                        val_mod_iter = self._batch_and_iterate(self._val_moderation_features)

                # # Reshape input and labels to the expected image dimensions
                # x = tf.reshape(x, shape=[-1, self._image_height, self._image_width, self._image_depth])
                # y = tf.reshape(y, shape=[-1, self._image_height, self._image_width, 1])
                #
                # # If we are using patching, we extract a random patch from the image here
                # if self._with_patching:
                #     x, offsets = self._graph_extract_patch(x)

            # Create an optimizer object for all of the devices
            optimizer = self._graph_make_optimizer()

            # Set up the graph layers
            self._log('Graph: Creating layer parameters...')
            self._add_layers_to_graph()

            # Do the forward pass and training output calcs on possibly multiple GPUs
            device_costs = []
            device_gradients = []
            device_variables = []
            for n, d in enumerate(self._get_device_list()):  # Build a graph on either the CPU or all of the GPUs
                with tf.device(d), tf.name_scope('tower_' + str(n)):
                    x, y = train_iter.get_next()

                    # Run the network operations
                    if self._has_moderation:
                        mod_w = train_mod_iter.get_next()
                        xx = self.forward_pass(x, deterministic=False, moderation_features=mod_w)
                    else:
                        xx = self.forward_pass(x, deterministic=False)
                    self._graph_forward_pass = xx  # Needed to output raw forward pass output to Tensorboard

                    # Define regularization cost
                    self._log('Graph: Calculating loss and gradients...')
                    if self._reg_coeff is not None:
                        l2_cost = tf.squeeze(tf.reduce_sum(
                            [layer.regularization_coefficient * tf.nn.l2_loss(layer.weights) for layer in self._layers
                             if isinstance(layer, layers.fullyConnectedLayer)]))
                    else:
                        l2_cost = 0.0

                    # Define cost function  based on which one was selected via set_loss_function
                    if self._loss_fn == 'sigmoid cross entropy':
                        pixel_loss = tf.nn.sigmoid_cross_entropy_with_logits(logits=xx, labels=y)
                    gpu_cost = tf.squeeze(tf.reduce_mean(pixel_loss) + l2_cost)
                    cost_sum = tf.reduce_sum(pixel_loss)
                    device_costs.append(cost_sum)

                    # Set the optimizer and get the gradients from it
                    gradients, variables, global_grad_norm = self._graph_get_gradients(gpu_cost, optimizer)
                    device_gradients.append(gradients)
                    device_variables.append(variables)

            # Average the gradients from each GPU and apply them
            average_gradients = self._graph_average_gradients(device_gradients)
            opt_variables = device_variables[0]
            self._graph_ops['optimizer'] = self._graph_apply_gradients(average_gradients, opt_variables, optimizer)

            # Average the costs and accuracies from each GPU
            self._graph_ops['cost'] = tf.reduce_sum(device_costs) / self._batch_size + l2_cost

            # Calculate test  and validation accuracy (on a single device at Tensorflow's discretion)
            # if self._testing:
            #     x_test = tf.reshape(x_test, shape=[-1, self._image_height, self._image_width, self._image_depth])
            #     self._graph_ops['y_test'] = tf.reshape(self._graph_ops['y_test'],
            #                                            shape=[-1, self._image_height, self._image_width, 1])
            # if self._validation:
            #     x_val = tf.reshape(x_val, shape=[-1, self._image_height, self._image_width, self._image_depth])
            #     self._graph_ops['y_val'] = tf.reshape(self._graph_ops['y_val'],
            #                                           shape=[-1, self._image_height, self._image_width, 1])
            # # If using patching, we need to properly pull similar patches from the test and validation images (and
            # # labels)
            # if self._with_patching:
            #     if self._testing:
            #         x_test, _ = self._graph_extract_patch(x_test, offsets)
            #         self._graph_ops['y_test'], _ = self._graph_extract_patch(self._graph_ops['y_test'], offsets)
            #     if self._validation:
            #         x_val, _ = self._graph_extract_patch(x_val, offsets)
            #         self._graph_ops['y_val'], _ = self._graph_extract_patch(self._graph_ops['y_val'], offsets)

            if self._testing:
                x_test, self._graph_ops['y_test'] = test_iter.get_next()

                if self._has_moderation:
                    mod_w_test = test_mod_iter.get_next()
                    self._graph_ops['x_test_predicted'] = self.forward_pass(x_test, deterministic=True,
                                                                            moderation_features=mod_w_test)
                else:
                    self._graph_ops['x_test_predicted'] = self.forward_pass(x_test, deterministic=True)

                self._graph_ops['test_losses'] = tf.reduce_mean(tf.nn.sigmoid_cross_entropy_with_logits(
                    logits=self._graph_ops['x_test_predicted'], labels=self._graph_ops['y_test']), axis=2)
                self._graph_ops['test_losses'] = tf.reshape(tf.reduce_mean(
                    self._graph_ops['test_losses'], axis=1), [self._batch_size])

            if self._validation:
                x_val, self._graph_ops['y_val'] = val_iter.get_next()

                if self._has_moderation:
                    mod_w_val = val_mod_iter.get_next()
                    self._graph_ops['x_val_predicted'] = self.forward_pass(x_val, deterministic=True,
                                                                           moderation_features=mod_w_val)
                else:
                    self._graph_ops['x_val_predicted'] = self.forward_pass(x_val, deterministic=True)

                self._graph_ops['val_losses'] = tf.reduce_mean(tf.nn.sigmoid_cross_entropy_with_logits(
                    logits=self._graph_ops['x_val_predicted'], labels=self._graph_ops['y_val']), axis=2)
                self._graph_ops['val_losses'] = tf.reshape(tf.reduce_mean(
                    self._graph_ops['val_losses'], axis=1), [self._batch_size])
                self._graph_ops['val_cost'] = tf.reduce_mean(self._graph_ops['val_losses'])

            # Epoch summaries for Tensorboard
            if self._tb_dir is not None:
                self._graph_tensorboard_summary(l2_cost, gradients, variables, global_grad_norm)

    def compute_full_test_accuracy(self):
        self._log('Computing total test accuracy/regression loss...')

        with self._graph.as_default():
            num_batches = int(np.ceil(self._total_testing_samples / self._batch_size))

            if num_batches == 0:
                warnings.warn('Less than a batch of testing data')
                exit()

            # Initialize storage for the retrieved test variables
            all_losses = []

            # Main test loop
            for _ in tqdm(range(num_batches)):
                r_losses = self._session.run(self._graph_ops['test_losses'])
                all_losses.append(r_losses)

            all_losses = np.concatenate(all_losses, axis=0)

            # For semantic segmentation problems we want relative and abs mean, std of L2 norms, plus a histogram of
            # errors
            abs_mean = np.mean(np.abs(all_losses))
            abs_var = np.var(np.abs(all_losses))
            abs_std = np.sqrt(abs_var)

            mean = np.mean(all_losses)
            var = np.var(all_losses)
            mse = np.mean(np.square(all_losses))
            std = np.sqrt(var)
            loss_max = np.amax(all_losses)
            loss_min = np.amin(all_losses)

            hist, _ = np.histogram(all_losses, bins=100)

            self._log('Mean loss: {}'.format(mean))
            self._log('Loss standard deviation: {}'.format(std))
            self._log('Mean absolute loss: {}'.format(abs_mean))
            self._log('Absolute loss standard deviation: {}'.format(abs_std))
            self._log('Min error: {}'.format(loss_min))
            self._log('Max error: {}'.format(loss_max))
            self._log('MSE: {}'.format(mse))

            self._log('Histogram of {} losses:'.format(self._loss_fn))
            self._log(hist)

            return abs_mean.astype(np.float32)

    def forward_pass_with_file_inputs(self, images):
        with self._graph.as_default():
            if self._with_patching:
                # we want the largest multiple of of patch height/width that is smaller than the original
                # image height/width, for the final image dimensions
                patch_height = self._patch_height
                patch_width = self._patch_width
                final_height = (self._image_height // patch_height) * patch_height
                final_width = (self._image_width // patch_width) * patch_width
                # find image differences to determine re-centering crop coords, we divide by 2 so that the leftover
                # is equal on all sides of image
                offset_height = (self._image_height - final_height) // 2
                offset_width = (self._image_width - final_width) // 2

            num_batches = len(images) // self._batch_size
            if len(images) % self._batch_size != 0:
                num_batches += 1

            self._parse_images(images)
            im_data = self._all_images.batch(self._batch_size).prefetch(1)
            x_test = im_data.make_one_shot_iterator().get_next()

            if self._load_from_saved:
                self.load_state()

            # Break images up into patches if necessary
            if self._with_patching:
                x_test = tf.image.crop_to_bounding_box(x_test, offset_height, offset_width, final_height, final_width)
                # Split the images up into the multiple slices of size patch_height x patch_width
                ksizes = [1, patch_height, patch_width, 1]
                strides = [1, patch_height, patch_width, 1]
                rates = [1, 1, 1, 1]
                x_test = tf.extract_image_patches(x_test, ksizes, strides, rates, "VALID")
                x_test = tf.reshape(x_test, shape=[-1, patch_height, patch_width, self._image_depth])

            # Run model on them
            x_pred = self.forward_pass(x_test, deterministic=True)

            total_outputs = []
            if self._with_patching:
                num_patch_rows = final_height // patch_height
                num_patch_cols = final_width // patch_width
                n_patches = num_patch_rows * num_patch_cols
                for i in range(num_batches):
                    xx = self._session.run(x_pred)

                    for img_patches in np.array_split(xx, xx.shape[0] / n_patches):
                        # Stitch individual rows together, than stitch the rows into a full image
                        full_img = []
                        for col_patches in np.array_split(img_patches, n_patches / num_patch_rows):
                            row_patches = [col_patches[i] for i in range(num_patch_cols)]
                            full_img.append(np.concatenate(row_patches, axis=1))
                        full_img = np.concatenate(full_img, axis=0)

                        # Keep the final image, but with an extra dimension to concatenate the images together
                        total_outputs.append(np.expand_dims(full_img, axis=0))
            else:
                for i in range(num_batches):
                    xx = self._session.run(x_pred)
                    for img_patches in np.array_split(xx, xx.shape[0]):
                        total_outputs.append(img_patches)

            total_outputs = np.concatenate(total_outputs, axis=0)

        return total_outputs

    def forward_pass_with_interpreted_outputs(self, x):
        total_outputs = self.forward_pass_with_file_inputs(x)

        # normalize and then threshold
        interpreted_outputs = np.zeros(total_outputs.shape, dtype=np.uint8)
        for i, img in enumerate(total_outputs):
            # normalize
            x_min = np.min(img)
            x_max = np.max(img)
            mask = (img - x_min) / (x_max - x_min)
            # threshold
            mask[mask >= 0.5] = 255
            mask[mask < 0.5] = 0
            # store
            interpreted_outputs[i, :, :] = mask

        return interpreted_outputs

    def add_output_layer(self, regularization_coefficient=None, output_size=None):
        if len(self._layers) < 1:
            raise RuntimeError("An output layer cannot be the first layer added to the model. " +
                               "Add an input layer with DPPModel.add_input_layer() first.")
        if regularization_coefficient is not None:
            warnings.warn("Semantic segmentation doesn't use regularization_coefficient in its output layer")
        if output_size is not None:
            raise RuntimeError("output_size should be None for semantic segmentation")

        self._log('Adding output layer...')

        filter_dimension = [1, 1, copy.deepcopy(self._last_layer().output_size[3]), 1]

        with self._graph.as_default():
            layer = layers.convLayer('output',
                                     copy.deepcopy(self._last_layer().output_size),
                                     filter_dimension,
                                     1,
                                     None,
                                     self._weight_initializer)

        self._log('Inputs: {0} Outputs: {1}'.format(layer.input_size, layer.output_size))
        self._layers.append(layer)

    def load_dataset_from_directory_with_segmentation_masks(self, dirname, seg_dirname):
        """
        Loads the png images in the given directory into an internal representation, using binary segmentation
        masks from another file with the same filename as ground truth.

        :param dirname: the path of the directory containing the images
        :param seg_dirname: the path of the directory containing ground-truth binary segmentation masks
        """

        image_files = [os.path.join(dirname, name) for name in os.listdir(dirname) if
                       os.path.isfile(os.path.join(dirname, name)) & name.endswith('.png')]

        seg_files = [os.path.join(seg_dirname, name) for name in os.listdir(seg_dirname) if
                     os.path.isfile(os.path.join(seg_dirname, name)) & name.endswith('.png')]

        self._total_raw_samples = len(image_files)

        self._log('Total raw examples is %d' % self._total_raw_samples)

        self._raw_image_files = image_files
        self._raw_labels = seg_files
        self._split_labels = False  # Band-aid fix

    def _parse_apply_preprocessing(self, images, labels):
        # Apply pre-processing to the image labels too (which are images for semantic segmentation)
        images = self._parse_read_images(images, channels=self._image_depth)
        labels = self._parse_read_images(labels, channels=1)
        return images, labels

    def _parse_resize_images(self, images, labels):
        images = tf.image.resize_images(images, [self._image_height, self._image_width])
        labels = tf.image.resize_images(labels, [self._image_height, self._image_width])
        return images, labels

    def _parse_crop_or_pad(self, images, labels):
        images = tf.image.resize_image_with_crop_or_pad(images, self._image_height, self._image_width)
        labels = tf.image.resize_image_with_crop_or_pad(labels, self._image_height, self._image_width)
        return images, labels

    def _parse_force_set_shape(self, images, labels):
        images.set_shape([self._image_height, self._image_width, self._image_depth])
        labels.set_shape([self._image_height, self._image_width, 1])
        return images, labels
