"""
Implementation of `3.1 Appearance-based Gaze Estimation` from
[Learning from Simulated and Unsupervised Images through Adversarial Training](https://arxiv.org/pdf/1612.07828v1.pdf).

Note: Only Python 3 support currently.
"""
import sys

from keras import applications
from keras import layers
from keras import models
from keras.preprocessing import image
import numpy as np
import tensorflow as tf

#
# Temporary workarounds
#
DISC_SOFTMAX_OUTPUT_DIM = 252  # FIXME: is this correct?

#
# image dimensions
#
img_width = 55
img_height = 35
img_channels = 1

#
# training params
#
nb_steps = 10000
batch_size = 32
k_d = 1  # number of discriminator updates per step
k_g = 2  # number of generative network updates per step

#
# refined image history buffer
#
refined_image_history_buffer = np.zeros(shape=(0, img_height, img_width, img_channels))
history_buffer_max_size = batch_size * 1000  # TODO: what should the size of this buffer be?


def add_half_batch_to_image_history(half_batch_generated_images):
    global refined_image_history_buffer
    assert len(half_batch_generated_images) == batch_size / 2

    if len(refined_image_history_buffer) < history_buffer_max_size:
        refined_image_history_buffer = np.concatenate((refined_image_history_buffer, half_batch_generated_images))
    elif len(refined_image_history_buffer) == history_buffer_max_size:
        refined_image_history_buffer[:batch_size // 2] = half_batch_generated_images
    else:
        assert False

    np.random.shuffle(refined_image_history_buffer)


def get_half_batch_from_image_history():
    global refined_image_history_buffer

    try:
        return refined_image_history_buffer[:batch_size // 2]
    except IndexError:
        return np.zeros(shape=(0, img_height, img_width, img_channels))


def refiner_network(input_image_tensor):
    """
    The refiner network, Rθ, is a residual network (ResNet). It modifies the synthetic image on a pixel level, rather
    than holistically modifying the image content, preserving the global structure and annotations.

    :param input_image_tensor: Input tensor that corresponds to a synthetic image.
    :return: Output tensor that corresponds to a refined synthetic image.
    """
    def resnet_block(input_features, nb_features=64, nb_kernel_rows=3, nb_kernel_cols=3):
        """
        A ResNet block with two `nb_kernel_rows` x `nb_kernel_cols` convolutional layers,
        each with `nb_features` feature maps.

        See Figure 6 in https://arxiv.org/pdf/1612.07828v1.pdf.

        :param input_features: Input tensor to ResNet block.
        :return: Output tensor from ResNet block.
        """
        y = layers.Convolution2D(nb_features, nb_kernel_rows, nb_kernel_cols, border_mode='same')(input_features)
        y = layers.Activation('relu')(y)
        y = layers.Convolution2D(nb_features, nb_kernel_rows, nb_kernel_cols, border_mode='same')(y)

        y = layers.merge([input_features, y], mode='sum')
        return layers.Activation('relu')(y)

    # an input image of size w × h is convolved with 3 × 3 filters that output 64 feature maps
    x = layers.Convolution2D(64, 3, 3, border_mode='same')(input_image_tensor)

    # the output is passed through 4 ResNet blocks
    for i in range(4):
        x = resnet_block(x)

    # the output of the last ResNet block is passed to a 1 × 1 convolutional layer producing 1 feature map
    # corresponding to the refined synthetic image
    return layers.Convolution2D(1, 1, 1, border_mode='same')(x)


def discriminator_network(input_image_tensor):
    """
    The discriminator network, Dφ, contains 5 convolution layers and 2 max-pooling layers.

    :param input_image_tensor: Input tensor corresponding to an image, either real or refined.
    :return: Output tensor that corresponds to the probability of whether an image is real or refined.
    """
    x = layers.Convolution2D(96, 3, 3, border_mode='same', subsample=(2, 2))(input_image_tensor)
    x = layers.Convolution2D(64, 3, 3, border_mode='same', subsample=(2, 2))(x)
    x = layers.MaxPooling2D(pool_size=(3, 3), strides=(1, 1), border_mode='same')(x)
    x = layers.Convolution2D(32, 3, 3, border_mode='same', subsample=(1, 1))(x)
    x = layers.Convolution2D(32, 1, 1, border_mode='same', subsample=(1, 1))(x)
    x = layers.Convolution2D(2, 1, 1, border_mode='same', subsample=(1, 1))(x)

    x = layers.Reshape((DISC_SOFTMAX_OUTPUT_DIM, ))(x)
    return layers.Activation('softmax', name='disc_softmax')(x)


def adversarial_training(synthesis_eyes_dir, mpii_gaze_dir):
    """Adversarial training of refiner network Rθ."""
    #
    # define model inputs and outputs
    #
    synthetic_image_tensor = layers.Input(shape=(img_height, img_width, img_channels))
    refined_image_tensor = refiner_network(synthetic_image_tensor)

    refined_or_real_image_tensor = layers.Input(shape=(img_height, img_width, img_channels))
    discriminator_output = discriminator_network(refined_or_real_image_tensor)

    combined_output = discriminator_network(refiner_network(synthetic_image_tensor))

    #
    # define models
    #
    refiner_model = models.Model(input=synthetic_image_tensor, output=refined_image_tensor, name='refiner')
    discriminator_model = models.Model(input=refined_or_real_image_tensor, output=discriminator_output,
                                       name='discriminator')
    combined_model = models.Model(input=synthetic_image_tensor, output=[refined_image_tensor, combined_output],
                                  name='combined')

    #
    # define custom l1 loss function for the refiner
    #
    def self_regularization_loss(y_true, y_pred):
        delta = 0.001  # FIXME: need to find ideal value for this
        return tf.multiply(delta, tf.reduce_sum(tf.abs(y_pred - y_true)))

    #
    # compile models
    #
    refiner_model.compile(optimizer='adam', loss=self_regularization_loss)
    discriminator_model.compile(optimizer='adam', loss='categorical_crossentropy')
    discriminator_model.trainable = False
    combined_model.compile(optimizer='adam', loss=[self_regularization_loss, 'categorical_crossentropy'])

    #
    # data generators
    #
    datagen = image.ImageDataGenerator(
        preprocessing_function=applications.xception.preprocess_input,
        dim_ordering='tf')

    flow_from_directory_params = {'target_size': (img_height, img_width),
                                  'color_mode': 'grayscale' if img_channels == 1 else 'rgb',
                                  'class_mode': None,
                                  'batch_size': batch_size}

    synthetic_generator = datagen.flow_from_directory(
        directory=synthesis_eyes_dir,
        **flow_from_directory_params
    )

    real_generator = datagen.flow_from_directory(
        directory=mpii_gaze_dir,
        **flow_from_directory_params
    )

    def get_image_batch(generator):
        """keras generators may generate an incomplete batch for the last batch"""
        img_batch = generator.next()
        if len(img_batch) != batch_size:
            img_batch = generator.next()

        return img_batch

    # the target labels for the cross-entropy loss layer are 0 for every yj and 1 for every xi
    y_real = np.zeros(shape=(batch_size, DISC_SOFTMAX_OUTPUT_DIM))
    y_refined = np.ones(shape=(batch_size, DISC_SOFTMAX_OUTPUT_DIM))

    # we first train the Rθ network with just self-regularization loss for 1,000 steps
    print('pre-training the refiner network...')
    for i in range(1000):
        print(i)
        image_batch = get_image_batch(synthetic_generator)
        refiner_model.train_on_batch(image_batch, image_batch)

    # and Dφ for 200 steps (one mini-batch for refined images, another for real)
    print('pre-training the discriminator network...')
    for i in range(100):
        print(i)
        real_image_batch = get_image_batch(real_generator)
        discriminator_model.train_on_batch(real_image_batch, y_real)

        synthetic_image_batch = get_image_batch(synthetic_generator)
        refined_image_batch = refiner_model.predict(synthetic_image_batch)
        discriminator_model.train_on_batch(refined_image_batch, y_refined)

    # see Algorithm 1 in https://arxiv.org/pdf/1612.07828v1.pdf
    for i in range(nb_steps):
        print('Step: {} of {}.'.format(i, nb_steps))

        # train the refiner
        for _ in range(k_g * 2):
            # sample a mini-batch of synthetic images
            synthetic_image_batch = get_image_batch(synthetic_generator)

            # update θ by taking an SGD step on mini-batch loss LR(θ)
            combined_model.train_on_batch(synthetic_image_batch, [synthetic_image_batch, y_real])

        for _ in range(k_d):
            # sample a mini-batch of synthetic and real images
            synthetic_image_batch = get_image_batch(synthetic_generator)
            real_image_batch = get_image_batch(real_generator)

            # refine the synthetic images w/ the current refiner
            refined_image_batch = refiner_model.predict(synthetic_image_batch)

            # use a history of refined images
            half_batch_from_image_history = get_half_batch_from_image_history()
            add_half_batch_to_image_history(refined_image_batch[:batch_size // 2])

            try:
                refined_image_batch[:batch_size // 2] = half_batch_from_image_history[:batch_size // 2]
            except IndexError as e:
                print(e)
                pass

            # update φ by taking an SGD step on mini-batch loss LD(φ)
            discriminator_model.train_on_batch(real_image_batch, y_real)
            discriminator_model.train_on_batch(refined_image_batch, y_refined)


def main(synthesis_eyes_dir, mpii_gaze_dir):
    adversarial_training(synthesis_eyes_dir, mpii_gaze_dir)


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])