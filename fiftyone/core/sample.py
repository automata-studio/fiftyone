"""
Core definitions of FiftyOne dataset samples.

| Copyright 2017-2020, Voxel51, Inc.
| `voxel51.com <https://voxel51.com/>`_
|
"""
# pragma pylint: disable=redefined-builtin
# pragma pylint: disable=unused-wildcard-import
# pragma pylint: disable=wildcard-import
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals
from builtins import *

# pragma pylint: enable=redefined-builtin
# pragma pylint: enable=unused-wildcard-import
# pragma pylint: enable=wildcard-import

import os

from mongoengine.errors import InvalidDocumentError

import eta.core.image as etai

import fiftyone.core.document as fod
import fiftyone.core.odm as foo


class Sample(fod.BackedByDocument):
    """A sample in a :class:`fiftyone.core.dataset.Dataset`.

    Samples store all information associated with a particular piece of data in
    a dataset, including basic metadata about the data, one or more sets of
    labels (ground truth, user-provided, or FiftyOne-generated), and additional
    features associated with subsets of the data and/or label sets.
    """

    _ODM_DOCUMENT_CLS = foo.ODMSample

    def __init__(self, document):
        super(Sample, self).__init__(document)
        self._dataset = None

    @classmethod
    def create_new(cls, filepath, tags=None, insights=None, labels=None):
        """Creates a new :class:`Sample`.

        Args:
            filepath: the path to the data on disk
            tags (None): the set of tags associated with the sample
            insights (None): a list of :class:`fiftyone.core.insights.Insight`
                instances associated with the sample
            labels (None): a list of :class:`fiftyone.core.labels.Label`
                instances associated with the sample
        """
        return cls._create_new(
            filepath=os.path.abspath(filepath),
            tags=tags,
            insights=insights,
            labels=labels,
        )

    @property
    def dataset(self):
        """The name of the dataset to which this sample belongs, or ``None`` if
        it has not been added to a dataset.
        """
        return self._dataset.name if self._dataset is not None else None

    @property
    def filepath(self):
        """The path to the raw data on disk."""
        return self._doc.filepath

    @property
    def filename(self):
        """The name of the raw data file on disk."""
        return os.path.basename(self.filepath)

    @property
    def tags(self):
        """The list of tags attached to the sample."""
        return self._doc.tags

    @property
    def insights(self):
        """The list of insights attached to the sample."""
        return self._doc.insights

    @property
    def labels(self):
        """The list of labels attached to the sample."""
        return self._doc.labels

    def add_tag(self, tag):
        """Adds the given tag to the sample only if it is not already there.

        Args:
            tag: the tag

        Returns:
            True on success (even if tag is not added)

        Raises:
            fiftyone.core.odm.DoesNotExist if the sample has been deleted
        """
        try:
            if not self._doc.modify(add_to_set__tags=tag):
                self._doc.reload()  # this will raise a DoesNotExist error
        except InvalidDocumentError:
            # document not in the database, add tag locally
            if tag not in self.tags:
                self._doc.tags.append("train")

        return True

    def remove_tag(self, tag):
        """Adds the given tag to the sample.

        Args:
            tag: the tag

        Returns:
            True on success (even if tag is not removed)

        Raises:
            fiftyone.core.odm.DoesNotExist if the sample has been deleted
        """
        try:
            if not self._doc.modify(pull__tags=tag):
                self._doc.reload()  # this will raise a DoesNotExist error
        except InvalidDocumentError:
            # document not in the database, remove tag locally
            if tag in self.tags:
                self.tags.pop(self.tags.index(tag))

        return True

    # def add_insight(self, group, insight):
    #     """Adds the given insight to the sample.
    #
    #     Args:
    #         insight: a :class:`fiftyone.core.insights.Insight` instance
    #     """
    #     # @todo(Tyler) this needs to write to the DB
    #     self._insights[group] = insight
    #
    # def add_label(self, group, label):
    #     """Adds the given label to the sample.
    #
    #     Args:
    #         label: a :class:`fiftyone.core.label.Label` instance
    #     """
    #     # @todo(Tyler) this needs to write to the DB
    #     self._dataset._validate_label(group, label)
    #     self._labels[group] = label

    def _set_dataset(self, dataset):
        self._doc.dataset = dataset.name
        self._dataset = dataset


class ImageSample(Sample):
    """An image sample in a :class:`fiftyone.core.dataset.Dataset`.

    The data associated with ``ImageSample`` instances are images.
    """

    _ODM_DOCUMENT_CLS = foo.ODMImageSample

    @classmethod
    def create_new(
        cls, filepath, tags=None, insights=None, labels=None, metadata=None
    ):
        """Creates a new :class:`ImageSample`.

        Args:
            filepath: the path to the image on disk
            tags (None): the set of tags associated with the sample
            insights (None): a list of :class:`fiftyone.core.insights.Insight`
                instances associated with the sample
            labels (None): a list of :class:`fiftyone.core.labels.Label`
                instances associated with the sample
            metadata (None): an ``eta.core.image.ImageMetadata`` instance for
                the image
        """
        if metadata is None:
            # WARNING: this reads the image from disk, so will be slow...
            metadata = etai.ImageMetadata.build_for(filepath)

        return cls._create_new(
            filepath=os.path.abspath(filepath),
            tags=tags,
            insights=insights,
            labels=labels,
            size_bytes=metadata.size_bytes,
            mime_type=metadata.mime_type,
            width=metadata.frame_size[0],
            height=metadata.frame_size[1],
            num_channels=metadata.num_channels,
        )

    def load_image(self):
        """Loads the image for the sample.

        Returns:
            a numpy image
        """
        return etai.read(self.filepath)
