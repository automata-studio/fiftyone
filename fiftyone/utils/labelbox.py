"""
Utilities for working with annotations in
`Labelbox format <https://labelbox.com/docs/exporting-data/export-format-detail>`_.

| Copyright 2017-2021, Voxel51, Inc.
| `voxel51.com <https://voxel51.com/>`_
|
"""
from copy import copy, deepcopy
import logging
import os
from uuid import uuid4
import warnings
import webbrowser

import numpy as np

import eta.core.image as etai
import eta.core.serial as etas
import eta.core.utils as etau
import eta.core.web as etaw

import fiftyone.core.fields as fof
import fiftyone.core.labels as fol
import fiftyone.core.media as fomm
import fiftyone.core.metadata as fom
import fiftyone.core.sample as fos
import fiftyone.core.utils as fou
import fiftyone.utils.annotations as foua

lb = fou.lazy_import("labelbox")
lbs = fou.lazy_import("labelbox.schema.ontology")


logger = logging.getLogger(__name__)


class LabelboxBackendConfig(foua.AnnotationBackendConfig):
    """Base class for configuring :class:`LabelboxBackend` instances.

    Args:
        label_schema: a dictionary containing the description of label fields,
            classes and attribute to annotate
        media_field ("filepath"): string field name containing the paths to
            media files on disk to upload
        url (None): the url of the Labelbox server
        api_key (None): the Labelbox API key
        classes_as_attrs (True): whether to show every class at the top
            level of the editor (False) or whether to show the label field
            at the top level and annotate the class as a required
            attribute (True)
        project_name (None): the name of the project that will be created,
            defaults to FiftyOne_<dataset-name>
    """

    def __init__(
        self,
        label_schema,
        media_field="filepath",
        url=None,
        api_key=None,
        classes_as_attrs=True,
        project_name=None,
        **kwargs,
    ):
        super().__init__(
            "labelbox", label_schema, media_field=media_field, **kwargs
        )
        self.url = url
        self.classes_as_attrs = classes_as_attrs
        self.project_name = project_name

        # store privately so it isn't serialized
        self._api_key = api_key

    @property
    def api_key(self):
        return self._api_key

    @api_key.setter
    def api_key(self, value):
        self._api_key = value


class LabelboxBackend(foua.AnnotationBackend):
    """Class for interacting with the Labelbox annotation backend."""

    @property
    def supported_label_types(self):
        return {
            fol.Classification,
            fol.Classifications,
            fol.Detection,
            fol.Detections,
            fol.Keypoint,
            fol.Keypoints,
            fol.Polyline,
            fol.Polylines,
        }

    @property
    def supported_scalar_types(self):
        return {
            fof.IntField,
            fof.FloatField,
            fof.StringField,
            fof.BooleanField,
        }

    @property
    def supported_attr_types(self):
        return {"text", "select", "radio", "checkbox"}

    @property
    def default_attr_type(self):
        return "text"

    @property
    def default_categorical_attr_type(self):
        return None

    def requires_attr_values(self, attr_type):
        return False

    def connect_to_api(self):
        """Returns an API instance connected to the Labelbox server.

        Returns:
            a :class:`LabelboxAnnotationAPI`
        """
        return LabelboxAnnotationAPI(
            self.config.name, self.config.url, api_key=self.config.api_key,
        )

    def upload_annotations(self, samples, launch_editor=False):
        api = self.connect_to_api()

        project_id = api.upload_samples(
            samples,
            label_schema=self.config.label_schema,
            media_field=self.config.media_field,
            classes_as_attrs=self.config.classes_as_attrs,
            project_name=self.config.project_name,
        )

        if launch_editor:
            editor_url = api.editor_url(project_id)
            logger.info("Launching editor at '%s'...", editor_url)
            api.launch_editor(url=editor_url)

        return LabelboxAnnotationResults(
            samples, self.config, project_id, backend=self
        )

    def download_annotations(self, results):
        api = self.connect_to_api()

        logger.info("Downloading labels from Labelbox...")
        annotations = api.download_annotations(
            results.config.label_schema, results.project_id,
        )
        logger.info("Download complete")

        return annotations


class LabelboxAnnotationAPI(foua.AnnotationAPI):
    """A class to facilitate connection to and management of projects in
    Labelbox.

    On initializiation, this class constructs a client based on the provided
    server url and credentials.

    This API provides methods to easily upload, download, create, and delete
    projects and data through the formatted urls specified by the Labelbox API.

    Additionally, samples and label schemas can be uploaded and annotations
    downloaded through this class.

    Args:
        name: the name of the backend
        url: url of the Labelbox serverPI
        api_key (None): the Labelbox API key
    """

    def __init__(self, name, url, api_key=None):
        self._name = name
        if "://" not in url:
            protocol = "http"
            base_url = url
        else:
            protocol, base_url = url.split("://")
        self._url = base_url
        self._protocol = protocol
        self._api_key = api_key

        self._setup()

        self._tools_per_label_type = {
            "detections": [lbs.Tool.Type.BBOX],
            "detection": [lbs.Tool.Type.BBOX],
            "segmentation": [lbs.Tool.Type.SEGMENTATION],
            "segmentations": [lbs.Tool.Type.SEGMENTATION],
            "_segmentations_and_detections": [
                lbs.Tool.Type.BBOX,
                lbs.Tool.Type.SEGMENTATION,
            ],
            "_segmentation_and_detection": [
                lbs.Tool.Type.BBOX,
                lbs.Tool.Type.SEGMENTATION,
            ],
            "semantic_segmentations": [lbs.Tool.Type.SEGMENTATION],
            "polyline": [lbs.Tool.Type.LINE],
            "polylines": [lbs.Tool.Type.LINE],
            "polygon": [lbs.Tool.Type.POLYGON],
            "polygons": [lbs.Tool.Type.POLYGON],
            "classification": [lbs.Classification],
            "classifications": [lbs.Classification],
            "scalar": [lbs.Classification],
        }

    def _setup(self):
        if not self._url:
            raise ValueError(
                "You must provide/configure the `url` of the Labelbox server"
            )

        api_key = self._api_key

        if api_key is None:
            api_key = self._prompt_api_key(self._name, api_key=api_key)

        self._client = lb.client.Client(
            api_key=api_key, endpoint=self.base_graphql_url
        )

    @property
    def attr_type_map(self):
        return {
            "text": lbs.Classification.Type.TEXT,
            "select": lbs.Classification.Type.DROPDOWN,
            "radio": lbs.Classification.Type.RADIO,
            "checkbox": lbs.Classification.Type.CHECKLIST,
        }

    @property
    def base_api_url(self):
        return "%s://api.%s" % (self._protocol, self._url)

    @property
    def base_graphql_url(self):
        return "%s/graphql" % self.base_api_url

    @property
    def projects_url(self):
        return "%s/projects" % self.base_api_url

    def project_url(self, project_id):
        return "%s/%s" % (self.projects_url, project_id)

    def editor_url(self, project_id):
        return "%s://editor.%s/?project=%s" % (
            self._protocol,
            self._url,
            project_id,
        )

    def delete_project(self, project_id):
        """Deletes the given project from the Labelbox server.

        Args:
            project_id: the project id
        """
        logger.info("Deleting project %s...", str(project_id))
        project = self._client.get_project(project_id)
        project.delete()

    def delete_projects(self, project_ids):
        """Deletes the given projects from the Labelbox server.

        Args:
            project_ids: an iterable of project IDs
        """
        logger.info("Deleting projects...")
        with fou.ProgressBar() as pb:
            for project_id in pb(list(project_ids)):
                self.delete_project(project_id)

    @classmethod
    def upload_data(
        cls, samples, lb_client, lb_dataset, media_field="filepath"
    ):
        """
        Upload sample media to a given Labelbox client and dataset. This method
        uses ``labelbox.schema.dataset.Dataset.create_data_rows()`` to add
        data in batches and match the external id of the DataRow to the Sample
        id.

        Args:
            samples: a :class:`fiftyone.core.collections.SampleCollection`
                containing the media to upload
            lb_client: a ``labelbox.client.Client`` to which to upload the
                media files
            lb_dataset: a ``labelbox.schema.dataset.Dataset`` to which to
                add the media
            media_field ("filepath"): string field name containing the paths to
                media files on disk to upload

        Returns:
            the Labelbox dataset data row creation task
        """
        upload_info = []
        for sample in samples:
            media_path = sample[media_field]
            item_url = lb_client.upload_file(media_path)
            upload_info.append(
                {
                    lb.DataRow.row_data: item_url,
                    lb.DataRow.external_id: sample.id,
                }
            )

        task = lb_dataset.create_data_rows(upload_info)
        task.wait_till_done()

    def upload_samples(
        self,
        samples,
        label_schema,
        media_field="filepath",
        classes_as_attrs=True,
        project_name=None,
    ):
        """Parse the given samples and use the label schema to create project,
        upload data, and upload formatted annotations to Labelbox.

        Args:
            samples: a :class:`fiftyone.core.collections.SampleCollection` to
                upload to Labelbox
            label_schema: a dictionary containing the description of label
                fields, classes and attribute to annotate
            media_field ("filepath"): string field name containing the paths to
                media files on disk to upload
            classes_as_attrs (True): whether to show every class at the top
                level of the editor (False) or whether to show the label field
                at the top level and annotate the class as a required
                attribute (True)
            project_name (None): the name of the project that will be created,
                defaults to FiftyOne_<dataset-name>
        """
        if project_name is None:
            project_name = "FiftyOne_%s" % (
                samples._root_dataset.name.replace(" ", "_"),
            )

        dataset = self._client.create_dataset(name=project_name)
        self.upload_data(
            samples, self._client, dataset, media_field=media_field,
        )

        project_id = self._setup_project(
            project_name, dataset, label_schema, classes_as_attrs,
        )

        return project_id

    def _setup_project(
        self, project_name, dataset, label_schema, classes_as_attrs
    ):
        """Create a new Labelbox project, connect it to the dataset, and
        construct the ontology and editor for the given schema.
        """
        project = self._client.create_project(name=project_name)
        project.datasets.connect(dataset)
        self._setup_editor(project, label_schema, classes_as_attrs)

        if project.setup_complete is None:
            raise ValueError("Labelbox project failed to be created")

        return project.uid

    def _setup_editor(self, project, label_schema, classes_as_attrs):
        editor = next(
            self._client.get_labeling_frontends(
                where=lb.LabelingFrontend.name == "Editor"
            )
        )

        tools = []
        classifications = []

        for label_field, schema_info in label_schema.items():
            field_tools, field_classifications = self._create_ontology_tools(
                schema_info, label_field, classes_as_attrs
            )
            tools.extend(field_tools)
            classifications.extend(field_classifications)

        ontology_builder = lbs.OntologyBuilder(
            tools=tools, classifications=classifications
        )
        project.setup(editor, ontology_builder.asdict())

    def _create_ontology_tools(
        self, schema_info, label_field, classes_as_attrs
    ):
        tools = []
        classifications = []
        label_type = schema_info["type"]
        classes = schema_info["classes"]
        attr_schema = schema_info["attributes"]

        general_attrs = self._build_attributes(attr_schema)

        if label_type in ["scalar", "classification", "classifications"]:
            classifications = self._build_classifications(
                classes, label_field, general_attrs, label_type, label_field
            )

        else:
            tools = self._build_tools(
                classes,
                label_field,
                label_type,
                general_attrs,
                classes_as_attrs,
            )

        return tools, classifications

    def _build_attributes(self, attr_schema):
        attributes = []
        for attr_name, attr_info in attr_schema.items():
            attr_type = attr_info["type"]
            class_type = self.attr_type_map[attr_type]
            if attr_type == "text":
                attr = lbs.Classification(
                    class_type=class_type, instructions=attr_name,
                )
            else:
                attr_values = attr_info["values"]
                options = [lbs.Option(value=str(v)) for v in attr_values]
                attr = lbs.Classification(
                    class_type=class_type,
                    instructions=attr_name,
                    options=options,
                )
            attributes.append(attr)
        return attributes

    def _build_classifications(
        self, classes, name, general_attrs, label_type, label_field
    ):
        """Return the classifications for the given label field. Generally, the
        classification is a dropdown selection for given classes, but can be a
        text entry for scalars without provided classes.

        Attributes are available for Classification and Classifications types
        in nested dropdowns
        """
        classifications = []
        options = []
        for c in classes:
            if isinstance(c, dict):
                # raise NotImplementedError(
                #    "Class specific attributes are not yet supported"
                # )
                sub_classes = c["classes"]
                attrs = self._build_attributes(c["attributes"]) + general_attrs
            else:
                sub_classes = [c]
                attrs = general_attrs

            if label_type == "scalar":
                # Scalar fields cannot have attributes
                attrs = []

            for sc in sub_classes:
                if label_type == "scalar":
                    sub_attrs = attrs
                else:
                    # Multiple copies of attributes for different classes can
                    # get confusing, prefix each attribute with the class name
                    prefix = "%s_%s:" % (label_field, str(sc))
                    sub_attrs = deepcopy(attrs)
                    for attr in sub_attrs:
                        attr.instructions = prefix + attr.instructions

                options.append(lbs.Option(value=str(sc), options=sub_attrs))

        if label_type == "scalar" and not classes:
            classification = lbs.Classification(
                class_type=lbs.Classification.Type.TEXT, instructions=name,
            )
            classifications.append(classification)
        elif label_type == "classifications":
            classification = lbs.Classification(
                class_type=lbs.Classification.Type.CHECKLIST,
                instructions=name,
                options=options,
            )
            classifications.append(classification)
        else:
            classification = lbs.Classification(
                class_type=lbs.Classification.Type.RADIO,
                instructions=name,
                options=options,
            )
            classifications.append(classification)

        return classifications

    def _build_tools(
        self, classes, label_field, label_type, general_attrs, classes_as_attrs
    ):
        tools = []
        if not classes_as_attrs:
            for c in classes:
                if isinstance(c, dict):
                    # raise NotImplementedError(
                    #    "Class specific attributes are not yet supported"
                    # )
                    subset_classes = c["classes"]
                    subset_attr_schema = c["attributes"]
                    subset_attrs = self._build_attributes(subset_attr_schema)
                    all_attrs = general_attrs + subset_attrs
                    for sc in subset_classes:
                        tools.extend(
                            self._build_tool_for_class(
                                sc, label_type, all_attrs
                            )
                        )
                else:
                    tools.extend(
                        self._build_tool_for_class(
                            c, label_type, general_attrs
                        )
                    )
        else:
            tool_types = self._tools_per_label_type[label_type]
            attributes = self._create_classes_as_attrs(classes, general_attrs)
            for tool_type in tool_types:
                tools.append(
                    lbs.Tool(
                        name=label_field,
                        tool=tool_type,
                        classifications=attributes,
                    )
                )
        return tools

    def _build_tool_for_class(self, class_name, label_type, attributes):
        tools = []
        tool_types = self._tools_per_label_type[label_type]
        for tool_type in tool_types:
            tools.append(
                lbs.Tool(
                    name=str(class_name),
                    tool=tool_type,
                    classifications=attributes,
                )
            )
        return tools

    def _create_classes_as_attrs(self, classes, general_attrs):
        """Create a RADIO attribute for classes and format all class
        specific attributes.
        """
        options = []
        for c in classes:
            if isinstance(c, dict):
                # raise NotImplementedError(
                #    "Class specific attributes are not yet supported"
                # )
                subset_attrs = self._build_attributes(c["attributes"])
                for sc in c["classes"]:
                    options.append(
                        lbs.Option(value=str(sc), options=subset_attrs)
                    )

            else:
                options.append(lbs.Option(value=str(c)))
        classes_attr = lbs.Classification(
            class_type=lbs.Classification.Type.RADIO,
            instructions="class_name",
            options=options,
            required=True,
        )
        attributes = [classes_attr] + general_attrs
        return attributes

    def download_annotations(
        self, label_schema, project_id,
    ):
        """Download annotations from the Labelbox server and parses them into the
        appropriate FiftyOne types.

        Args:
            label_schema: a dictionary containing the description of label
                fields, classes and attribute to annotate
            project_id: the id of the project created by uploading samples

        Returns:
            the label results dict
        """
        # TODO
        return {}

    def _download_project_labels(
        self, project_id=None, project=None, timeout=60
    ):
        if project is None:
            if project_id is None:
                raise ValueError(
                    "Either `project_id` or `project` is required"
                )
            project = self._client.get_project(project_id)

        label_url = project.export_labels(timeout_seconds=timeout)
        return _download_or_load_ndjson(label_url)

    def launch_editor(self, url=None):
        """Launches the Labelbox editor in your default web browser.

        Args:
            url (None): an optional URL to open. By default, the base URL of
                the server is opened
        """
        if url is None:
            url = self.projects_url

        webbrowser.open(url, new=2)


class LabelboxAnnotationResults(foua.AnnotationResults):
    """Class that stores all relevant information needed to monitor the
    progress of an annotation run sent to Labelbox and download the results.

    Args:
        samples: a :class:`fiftyone.core.collections.SampleCollection`
        config: a :class:`LabelboxBackendConfig`
        project_id: the id string of the project created in this annotation run
        backend (None): a :class:`LabelboxBackend`
    """

    def __init__(
        self, samples, config, project_id, backend=None,
    ):
        super().__init__(samples, config, backend=backend)
        self.project_id = project_id

    def load_credentials(self, url=None, api_key=None):
        """Load the Labelbox credentials from the given keyword arguments or
        the FiftyOne annotation config.

        Args:
            url (None): the url of the Labelbox server
            api_key (None): the Labelbox API key
        """
        self._load_config_parameters(url=url, api_key=api_key)

    def get_status(self):
        """Gets the status of the annotation run.

        Returns:
            a dict of status information
        """
        return self._get_status()

    def print_status(self):
        """Print the status of the annotation run."""
        self._get_status(log=True)

    def connect_to_api(self):
        """Returns an API instance connected to the Labelbox server.

        Returns:
            a :class:`LabelboxAnnotationAPI`
        """
        return self._backend.connect_to_api()

    def cleanup(self):
        """Deletes the project associated with this annotation run from the
        Labelbox server.
        """
        if self.project_id is not None:
            api = self.connect_to_api()
            api.delete_project(self.project_id)

        # @todo save updated results to DB?
        self.project_id = None

    def _get_status(self, log=False):
        pass

    @classmethod
    def _from_dict(cls, d, samples, config):
        return cls(samples, config, d["project_id"])


#
# @todo
#   Must add support for populating `schemaId` when exporting
#   labels in order for model-assisted labeling to work properly
#
#   cf https://labelbox.com/docs/automation/model-assisted-labeling
#


def import_from_labelbox(
    dataset,
    json_path,
    label_prefix=None,
    download_dir=None,
    labelbox_id_field="labelbox_id",
):
    """Imports the labels from the Labelbox project into the FiftyOne dataset.

    The ``labelbox_id_field`` of the FiftyOne samples are used to associate the
    corresponding Labelbox labels.

    If a ``download_dir`` is provided, any Labelbox IDs with no matching
    FiftyOne sample are added to the FiftyOne dataset, and their media is
    downloaded into ``download_dir``.

    The provided ``json_path`` should contain a JSON file in the following
    format::

        [
            {
                "DataRow ID": <labelbox-id>,
                "Labeled Data": <url-or-None>,
                "Label": {...}
            }
        ]

    When importing image labels, the ``Label`` field should contain a dict of
    `Labelbox image labels <https://labelbox.com/docs/exporting-data/export-format-detail#images>`_::

        {
            "objects": [...],
            "classifications": [...]
        }

    When importing video labels, the ``Label`` field should contain a dict as
    follows::

        {
            "frames": <url-or-filepath>
        }

    where the ``frames`` field can either contain a URL, in which case the
    file is downloaded from the web, or the path to NDJSON file on disk of
    `Labelbox video labels <https://labelbox.com/docs/exporting-data/export-format-detail#video>`_::

        {"frameNumber": 1, "objects": [...], "classifications": [...]}
        {"frameNumber": 2, "objects": [...], "classifications": [...]}
        ...

    Args:
        dataset: a :class:`fiftyone.core.dataset.Dataset`
        json_path: the path to the Labelbox JSON export to load
        labelbox_project_or_json_path: a ``labelbox.schema.project.Project`` or
            the path to the JSON export of a Labelbox project on disk
        label_prefix (None): a prefix to prepend to the sample label field(s)
            that are created, separated by an underscore
        download_dir (None): a directory into which to download the media for
            any Labelbox IDs with no corresponding sample with the matching
            ``labelbox_id_field`` value. This can be omitted if all IDs are
            already present or you do not wish to download media and add new
            samples
        labelbox_id_field ("labelbox_id"): the sample field to lookup/store the
            IDs of the Labelbox DataRows
    """
    if download_dir:
        filename_maker = fou.UniqueFilenameMaker(output_dir=download_dir)

    if labelbox_id_field not in dataset.get_field_schema():
        dataset.add_sample_field(labelbox_id_field, fof.StringField)

    id_map = {k: v for k, v in zip(*dataset.values([labelbox_id_field, "id"]))}

    if label_prefix:
        label_key = lambda k: label_prefix + "_" + k
    else:
        label_key = lambda k: k

    is_video = dataset.media_type == fomm.VIDEO

    # Load labels
    d_list = etas.read_json(json_path)

    # ref: https://github.com/Labelbox/labelbox/blob/7c79b76310fa867dd38077e83a0852a259564da1/exporters/coco-exporter/coco_exporter.py#L33
    with fou.ProgressBar() as pb:
        for d in pb(d_list):
            labelbox_id = d["DataRow ID"]

            if labelbox_id in id_map:
                # Get existing sample
                sample = dataset[id_map[labelbox_id]]
            elif download_dir:
                # Download image and create new sample
                # @todo optimize by downloading images in a background thread
                # pool?
                image_url = d["Labeled Data"]
                filepath = filename_maker.get_output_path(image_url)
                etaw.download_file(image_url, path=filepath, quiet=True)
                sample = fos.Sample(filepath=filepath)
                dataset.add_sample(sample)
            else:
                logger.info(
                    "Skipping labels for unknown Labelbox ID '%s'; provide a "
                    "`download_dir` if you wish to download media and create "
                    "samples for new media",
                    labelbox_id,
                )
                continue

            if sample.metadata is None:
                if is_video:
                    sample.metadata = fom.VideoMetadata.build_for(
                        sample.filepath
                    )
                else:
                    sample.metadata = fom.ImageMetadata.build_for(
                        sample.filepath
                    )

            if is_video:
                frame_size = (
                    sample.metadata.frame_width,
                    sample.metadata.frame_height,
                )
                frames = _parse_video_labels(d["Label"], frame_size)
                sample.frames.merge(
                    {
                        frame_number: {
                            label_key(fname): flabel
                            for fname, flabel in frame_dict.items()
                        }
                        for frame_number, frame_dict in frames.items()
                    }
                )
            else:
                frame_size = (sample.metadata.width, sample.metadata.height)
                labels_dict = _parse_image_labels(d["Label"], frame_size)
                sample.update_fields(
                    {label_key(k): v for k, v in labels_dict.items()}
                )

            sample.save()


def export_to_labelbox(
    sample_collection,
    ndjson_path,
    video_labels_dir=None,
    labelbox_id_field="labelbox_id",
    label_field=None,
    frame_labels_field=None,
):
    """Exports labels from the FiftyOne samples to Labelbox format.

    This function is useful for loading predictions into Labelbox for
    `model-assisted labeling <https://labelbox.com/docs/automation/model-assisted-labeling>`_.

    You can use :meth:`upload_labels_to_labelbox` to upload the exported labels
    to a Labelbox project.

    You can use :meth:`upload_media_to_labelbox` to upload sample media to
    Labelbox and populate the ``labelbox_id_field`` field, if necessary.

    The IDs of the Labelbox DataRows corresponding to each sample must be
    stored in the ``labelbox_id_field`` of the samples. Any samples with no
    value in ``labelbox_id_field`` will be skipped.

    When exporting frame labels for video datasets, the ``frames`` key of the
    exported labels will contain the paths on disk to per-sample NDJSON files
    that are written to ``video_labels_dir`` as follows::

        video_labels_dir/
            <labelbox-id1>.json
            <labelbox-id2>.json
            ...

    where each NDJSON file contains the frame labels for the video with the
    corresponding Labelbox ID.

    Args:
        sample_collection: a
            :class:`fiftyone.core.collections.SampleCollection`
        ndjson_path: the path to write an NDJSON export of the labels
        video_labels_dir (None): a directory to write the per-sample video
            labels. Only applicable for video datasets
        labelbox_id_field ("labelbox_id"): the sample field to lookup/store the
            IDs of the Labelbox DataRows
        label_field (None): optional label field(s) to export. Can be any of
            the following:

            -   the name of a label field to export
            -   a glob pattern of label field(s) to export
            -   a list or tuple of label field(s) to export
            -   a dictionary mapping label field names to keys to use when
                constructing the exported labels

            By default, no labels are exported
        frame_labels_field (None): optional frame label field(s) to export.
            Only applicable to video datasets. Can be any of the following:

            -   the name of a frame label field to export
            -   a glob pattern of frame label field(s) to export
            -   a list or tuple of frame label field(s) to export
            -   a dictionary mapping frame label field names to keys to use
                when constructing the exported frame labels

            By default, no frame labels are exported
    """
    is_video = sample_collection.media_type == fomm.VIDEO

    # Get label fields to export
    label_fields = sample_collection._parse_label_field(
        label_field, allow_coersion=False, force_dict=True, required=False,
    )

    # Get frame label fields to export
    if is_video:
        frame_label_fields = sample_collection._parse_frame_labels_field(
            frame_labels_field,
            allow_coersion=False,
            force_dict=True,
            required=False,
        )

        if frame_label_fields and video_labels_dir is None:
            raise ValueError(
                "Must provide `video_labels_dir` when exporting frame labels "
                "for video datasets"
            )

    etau.ensure_empty_file(ndjson_path)

    # Export the labels
    with fou.ProgressBar() as pb:
        for sample in pb(sample_collection):
            labelbox_id = sample[labelbox_id_field]
            if labelbox_id is None:
                logger.warning(
                    "Skipping sample '%s' with no '%s' value",
                    sample.id,
                    labelbox_id_field,
                )
                continue

            # Compute metadata if necessary
            if sample.metadata is None:
                if is_video:
                    metadata = fom.VideoMetadata.build_for(sample.filepath)
                else:
                    metadata = fom.ImageMetadata.build_for(sample.filepath)

                sample.metadata = metadata
                sample.save()

            # Get frame size
            if is_video:
                frame_size = (
                    sample.metadata.frame_width,
                    sample.metadata.frame_height,
                )
            else:
                frame_size = (sample.metadata.width, sample.metadata.height)

            # Export sample-level labels
            if label_fields:
                labels_dict = _get_labels(sample, label_fields)
                annos = _to_labelbox_image_labels(
                    labels_dict, frame_size, labelbox_id
                )
                etas.write_ndjson(annos, ndjson_path, append=True)

            # Export frame-level labels
            if is_video and frame_label_fields:
                frames = _get_frame_labels(sample, frame_label_fields)
                video_annos = _to_labelbox_video_labels(
                    frames, frame_size, labelbox_id
                )

                video_labels_path = os.path.join(
                    video_labels_dir, labelbox_id + ".json"
                )
                etas.write_ndjson(video_annos, video_labels_path)

                anno = _make_video_anno(
                    video_labels_path, data_row_id=labelbox_id
                )
                etas.write_ndjson([anno], ndjson_path, append=True)


def download_labels_from_labelbox(labelbox_project, outpath=None):
    """Downloads the labels for the given Labelbox project.

    Args:
        labelbox_project: a ``labelbox.schema.project.Project``
        outpath (None): the path to write the JSON export on disk

    Returns:
        ``None`` if an ``outpath`` is provided, or the loaded JSON itself if no
        ``outpath`` is provided
    """
    export_url = labelbox_project.export_labels()

    if outpath:
        etaw.download_file(export_url, path=outpath)
        return None

    labels_bytes = etaw.download_file(export_url)
    return etas.load_json(labels_bytes)


def upload_media_to_labelbox(
    labelbox_dataset, sample_collection, labelbox_id_field="labelbox_id"
):
    """Uploads the raw media for the FiftyOne samples to Labelbox.

    The IDs of the Labelbox DataRows that are created are stored in the
    ``labelbox_id_field`` of the samples.

    Args:
        labelbox_dataset: a ``labelbox.schema.dataset.Dataset`` to which to
            add the media
        sample_collection: a
            :class:`fiftyone.core.collections.SampleCollection`
        labelbox_id_field ("labelbox_id"): the sample field in which to store
            the IDs of the Labelbox DataRows
    """
    # @todo use `create_data_rows()` to optimize performance
    # @todo handle API rate limits
    # Reference: https://labelbox.com/docs/python-api/data-rows
    with fou.ProgressBar() as pb:
        for sample in pb(sample_collection):
            try:
                has_id = sample[labelbox_id_field] is not None
            except:
                has_id = False

            if has_id:
                logger.warning(
                    "Skipping sample '%s' with an existing '%s' value",
                    sample.id,
                    labelbox_id_field,
                )
                continue

            filepath = sample.filepath
            data_row = labelbox_dataset.create_data_row(row_data=filepath)
            sample[labelbox_id_field] = data_row.uid
            sample.save()


def upload_labels_to_labelbox(
    labelbox_project, annos_or_ndjson_path, batch_size=None
):
    """Uploads labels to a Labelbox project.

    Use this function to load predictions into Labelbox for
    `model-assisted labeling <https://labelbox.com/docs/automation/model-assisted-labeling>`_.

    Use :meth:`export_to_labelbox` to export annotations in the format expected
    by this method.

    Args:
        labelbox_project: a ``labelbox.schema.project.Project``
        annos_or_ndjson_path: a list of annotation dicts or the path to an
            NDJSON file on disk containing annotations
        batch_size (None): an optional batch size to use when uploading the
            annotations. By default, ``annos_or_ndjson_path`` is passed
            directly to ``labelbox_project.upload_annotations()``
    """
    if batch_size is None:
        name = "%s-upload-request" % labelbox_project.name
        return labelbox_project.upload_annotations(name, annos_or_ndjson_path)

    if etau.is_str(annos_or_ndjson_path):
        annos = etas.read_ndjson(annos_or_ndjson_path)
    else:
        annos = annos_or_ndjson_path

    requests = []
    count = 0
    for anno_batch in fou.iter_batches(annos, batch_size):
        count += 1
        name = "%s-upload-request-%d" % (labelbox_project.name, count)
        request = labelbox_project.upload_annotations(name, anno_batch)
        requests.append(request)

    return requests


def convert_labelbox_export_to_import(inpath, outpath=None, video_outdir=None):
    """Converts a Labelbox NDJSON export generated by
    :meth:`export_to_labelbox` into the format expected by
    :meth:`import_from_labelbox`.

    The output JSON file will have the same format that is generated when
    `exporting a Labelbox project's labels <https://labelbox.com/docs/exporting-data/export-overview>`_.

    The ``Labeled Data`` fields of the output labels will be ``None``.

    Args:
        inpath: the path to an NDJSON file generated (for example) by
            :meth:`export_to_labelbox`
        outpath (None): the path to write a JSON file containing the converted
            labels. If omitted, the input file will be overwritten
        video_outdir (None): a directory to write the converted video frame
            labels (if applicable). If omitted, the input frame label files
            will be overwritten
    """
    if outpath is None:
        outpath = inpath

    din_list = etas.read_ndjson(inpath)

    dout_map = {}

    for din in din_list:
        uuid = din.pop("dataRow")["id"]
        din.pop("uuid")

        if "frames" in din:
            # Video annotation
            frames_inpath = din["frames"]

            # Convert frame labels
            if video_outdir is not None:
                frames_outpath = os.path.join(
                    video_outdir, os.path.basename(frames_inpath)
                )
            else:
                frames_outpath = frames_inpath

            _convert_labelbox_frames_export_to_import(
                frames_inpath, frames_outpath
            )

            dout_map[uuid] = {
                "DataRow ID": uuid,
                "Labeled Data": None,
                "Label": {"frames": frames_outpath},
            }
            continue

        if uuid not in dout_map:
            dout_map[uuid] = {
                "DataRow ID": uuid,
                "Labeled Data": None,
                "Label": {"objects": [], "classifications": []},
            }

        _ingest_label(din, dout_map[uuid]["Label"])

    dout = list(dout_map.values())
    etas.write_json(dout, outpath)


def _convert_labelbox_frames_export_to_import(inpath, outpath):
    din_list = etas.read_ndjson(inpath)

    dout_map = {}

    for din in din_list:
        frame_number = din.pop("frameNumber")
        din.pop("dataRow")
        din.pop("uuid")

        if frame_number not in dout_map:
            dout_map[frame_number] = {
                "frameNumber": frame_number,
                "objects": [],
                "classifications": [],
            }

        _ingest_label(din, dout_map[frame_number])

    dout = [dout_map[fn] for fn in sorted(dout_map.keys())]
    etas.write_ndjson(dout, outpath)


def _ingest_label(din, d_label):
    if any(k in din for k in ("bbox", "polygon", "line", "point", "mask")):
        # Object
        if "mask" in din:
            din["instanceURI"] = din.pop("mask")["instanceURI"]

        d_label["objects"].append(din)
    else:
        # Classification
        d_label["classifications"].append(din)


def _get_labels(sample_or_frame, label_fields):
    labels_dict = {}
    for field, key in label_fields.items():
        value = sample_or_frame[field]
        if value is not None:
            labels_dict[key] = value

    return labels_dict


def _get_frame_labels(sample, frame_label_fields):
    frames = {}
    for frame_number, frame in sample.frames.items():
        frames[frame_number] = _get_labels(frame, frame_label_fields)

    return frames


def _to_labelbox_image_labels(labels_dict, frame_size, data_row_id):
    annotations = []
    for name, label in labels_dict.items():
        if isinstance(label, (fol.Classification, fol.Classifications)):
            anno = _to_global_classification(name, label, data_row_id)
            annotations.append(anno)
        elif isinstance(label, (fol.Detection, fol.Detections)):
            annos = _to_detections(label, frame_size, data_row_id)
            annotations.extend(annos)
        elif isinstance(label, (fol.Polyline, fol.Polylines)):
            annos = _to_polylines(label, frame_size, data_row_id)
            annotations.extend(annos)
        elif isinstance(label, (fol.Keypoint, fol.Keypoints)):
            annos = _to_points(label, frame_size, data_row_id)
            annotations.extend(annos)
        elif isinstance(label, fol.Segmentation):
            annos = _to_mask(name, label, data_row_id)
            annotations.extend(annos)
        elif label is not None:
            msg = "Ignoring unsupported label type '%s'" % label.__class__
            warnings.warn(msg)

    return annotations


def _to_labelbox_video_labels(frames, frame_size, data_row_id):
    annotations = []
    for frame_number, labels_dict in frames.items():
        frame_annos = _to_labelbox_image_labels(
            labels_dict, frame_size, data_row_id
        )
        for anno in frame_annos:
            anno["frameNumber"] = frame_number
            annotations.append(anno)

    return annotations


# https://labelbox.com/docs/exporting-data/export-format-detail#classification
def _to_global_classification(name, label, data_row_id):
    anno = _make_base_anno(name, data_row_id=data_row_id)
    anno.update(_make_classification_answer(label))
    return anno


# https://labelbox.com/docs/exporting-data/export-format-detail#nested_classification
def _get_nested_classifications(label):
    classifications = []
    for name, value in label.iter_attributes():
        if etau.is_str(value) or isinstance(value, (list, tuple)):
            anno = _make_base_anno(name)
            anno.update(_make_classification_answer(value))
            classifications.append(anno)
        else:
            msg = "Ignoring unsupported attribute type '%s'" % type(value)
            warnings.warn(msg)
            continue

    return classifications


# https://labelbox.com/docs/automation/model-assisted-labeling#mask_annotations
def _to_mask(name, label, data_row_id):
    mask = np.asarray(label.mask)
    if mask.ndim < 3 or mask.dtype != np.uint8:
        raise ValueError(
            "Segmentation masks must be stored as RGB color uint8 images"
        )

    try:
        instance_uri = label.instance_uri
    except:
        raise ValueError(
            "You must populate the `instance_uri` field of segmentation masks"
        )

    # Get unique colors
    colors = np.unique(np.reshape(mask, (-1, 3)), axis=0).tolist()

    annos = []
    base_anno = _make_base_anno(name, data_row_id=data_row_id)
    for color in colors:
        anno = copy(base_anno)
        anno["mask"] = _make_mask(instance_uri, color)
        annos.append(anno)

    return annos


# https://labelbox.com/docs/exporting-data/export-format-detail#bounding_boxes
def _to_detections(label, frame_size, data_row_id):
    if isinstance(label, fol.Detections):
        detections = label.detections
    else:
        detections = [label]

    annos = []
    for detection in detections:
        anno = _make_base_anno(detection.label, data_row_id=data_row_id)
        anno["bbox"] = _make_bbox(detection.bounding_box, frame_size)

        classifications = _get_nested_classifications(detection)
        if classifications:
            anno["classifications"] = classifications

        annos.append(anno)

    return annos


# https://labelbox.com/docs/exporting-data/export-format-detail#polygons
# https://labelbox.com/docs/exporting-data/export-format-detail#polylines
def _to_polylines(label, frame_size, data_row_id):
    if isinstance(label, fol.Polylines):
        polylines = label.polylines
    else:
        polylines = [label]

    annos = []
    for polyline in polylines:
        field = "polygon" if polyline.filled else "line"
        classifications = _get_nested_classifications(polyline)
        for points in polyline.points:
            anno = _make_base_anno(polyline.label, data_row_id=data_row_id)
            anno[field] = [_make_point(point, frame_size) for point in points]
            if classifications:
                anno["classifications"] = classifications

            annos.append(anno)

    return annos


# https://labelbox.com/docs/exporting-data/export-format-detail#points
def _to_points(label, frame_size, data_row_id):
    if isinstance(label, fol.Keypoints):
        keypoints = label.keypoints
    else:
        keypoints = [keypoints]

    annos = []
    for keypoint in keypoints:
        classifications = _get_nested_classifications(keypoint)
        for point in keypoint.points:
            anno = _make_base_anno(keypoint.label, data_row_id=data_row_id)
            anno["point"] = _make_point(point, frame_size)
            if classifications:
                anno["classifications"] = classifications

            annos.append(anno)

    return annos


def _make_base_anno(value, data_row_id=None):
    anno = {
        "uuid": str(uuid4()),
        "schemaId": None,
        "title": value,
        "value": value,
    }

    if data_row_id:
        anno["dataRow"] = {"id": data_row_id}

    return anno


def _make_video_anno(labels_path, data_row_id=None):
    anno = {
        "uuid": str(uuid4()),
        "frames": labels_path,
    }

    if data_row_id:
        anno["dataRow"] = {"id": data_row_id}

    return anno


def _make_classification_answer(label):
    if isinstance(label, fol.Classification):
        # Assume free text
        return {"answer": label.label}

    if isinstance(label, fol.Classifications):
        # Assume checklist
        return {"answers": [{"value": c.label} for c in label.classifications]}

    if etau.is_str(label):
        # Assume free text
        return {"answer": label}

    if isinstance(label, (list, tuple)):
        # Assume checklist
        return {"answers": [{"value": value} for value in label]}

    raise ValueError("Cannot convert %s to a classification" % label.__class__)


def _make_bbox(bounding_box, frame_size):
    x, y, w, h = bounding_box
    width, height = frame_size
    return {
        "left": round(x * width, 1),
        "top": round(y * height, 1),
        "width": round(w * width, 1),
        "height": round(h * height, 1),
    }


def _make_point(point, frame_size):
    x, y = point
    width, height = frame_size
    return {"x": round(x * width, 1), "y": round(y * height, 1)}


def _make_mask(instance_uri, color):
    return {
        "instanceURI": instance_uri,
        "colorRGB": list(color),
    }


# https://labelbox.com/docs/exporting-data/export-format-detail#video
def _parse_video_labels(video_label_d, frame_size):
    url_or_filepath = video_label_d["frames"]
    label_d_list = _download_or_load_ndjson(url_or_filepath)

    frames = {}
    for label_d in label_d_list:
        frame_number = label_d["frameNumber"]
        frames[frame_number] = _parse_image_labels(label_d, frame_size)

    return frames


# https://labelbox.com/docs/exporting-data/export-format-detail#images
def _parse_image_labels(label_d, frame_size):
    labels = {}

    # Parse classifications
    cd_list = label_d.get("classifications", [])
    classifications = _parse_classifications(cd_list)
    labels.update(classifications)

    # Parse objects
    # @todo what if `objects.keys()` conflicts with `classifications.keys()`?
    od_list = label_d.get("objects", [])
    objects = _parse_objects(od_list, frame_size)
    labels.update(objects)

    return labels


def _parse_classifications(cd_list):
    labels = {}

    for cd in cd_list:
        name = cd["value"]
        if "answer" in cd:
            answer = cd["answer"]
            if isinstance(answer, list):
                # Dropdown
                labels[name] = fol.Classifications(
                    classifications=[
                        fol.Classification(label=a["value"]) for a in answer
                    ]
                )
            elif isinstance(answer, dict):
                # Radio question
                labels[name] = fol.Classification(label=answer["value"])
            else:
                # Free text
                labels[name] = fol.Classification(label=answer)

        if "answers" in cd:
            # Checklist
            answers = cd["answers"]
            labels[name] = fol.Classifications(
                classifications=[
                    fol.Classification(label=a["value"]) for a in answers
                ]
            )

    return labels


def _parse_attributes(cd_list):
    attributes = {}

    for cd in cd_list:
        name = cd["value"]
        if "answer" in cd:
            answer = cd["answer"]
            if isinstance(answer, list):
                # Dropdown
                attributes[name] = [a["value"] for a in answer]
            elif isinstance(answer, dict):
                # Radio question
                attributes[name] = answer["value"]
            else:
                # Free text
                attributes[name] = answer

        if "answers" in cd:
            # Checklist
            answer = cd["answers"]
            attributes[name] = [a["value"] for a in answer]

    return attributes


def _parse_objects(od_list, frame_size):
    detections = []
    polylines = []
    keypoints = []
    mask = None
    mask_instance_uri = None
    for od in od_list:
        label = od["value"]
        attributes = _parse_attributes(od.get("classifications", []))

        if "bbox" in od:
            # Detection
            bounding_box = _parse_bbox(od["bbox"], frame_size)
            detections.append(
                fol.Detection(
                    label=label, bounding_box=bounding_box, **attributes
                )
            )
        elif "polygon" in od:
            # Polyline
            points = _parse_points(od["polygon"], frame_size)
            polylines.append(
                fol.Polyline(
                    label=label,
                    points=[points],
                    closed=True,
                    filled=True,
                    **attributes,
                )
            )
        elif "line" in od:
            # Polyline
            points = _parse_points(od["line"], frame_size)
            polylines.append(
                fol.Polyline(
                    label=label,
                    points=[points],
                    closed=True,
                    filled=False,
                    **attributes,
                )
            )
        elif "point" in od:
            # Polyline
            point = _parse_point(od["point"], frame_size)
            keypoints.append(
                fol.Keypoint(label=label, points=[point], **attributes)
            )
        elif "instanceURI" in od:
            # Segmentation mask
            if mask is None:
                mask_instance_uri = od["instanceURI"]
                mask = _parse_mask(mask_instance_uri)
            elif od["instanceURI"] != mask_instance_uri:
                msg = (
                    "Only one segmentation mask per image/frame is allowed; "
                    "skipping additional mask(s)"
                )
                warnings.warn(msg)
        else:
            msg = "Ignoring unsupported label"
            warnings.warn(msg)

    labels = {}

    if detections:
        labels["detections"] = fol.Detections(detections=detections)

    if polylines:
        labels["polylines"] = fol.Polylines(polylines=polylines)

    if keypoints:
        labels["keypoints"] = fol.Keypoints(keypoints=keypoints)

    if mask is not None:
        labels["segmentation"] = mask

    return labels


def _parse_bbox(bd, frame_size):
    width, height = frame_size
    x = bd["left"] / width
    y = bd["top"] / height
    w = bd["width"] / width
    h = bd["height"] / height
    return [x, y, w, h]


def _parse_points(pd_list, frame_size):
    return [_parse_point(pd, frame_size) for pd in pd_list]


def _parse_point(pd, frame_size):
    width, height = frame_size
    return (pd["x"] / width, pd["y"] / height)


def _parse_mask(instance_uri):
    img_bytes = etaw.download_file(instance_uri, quiet=True)
    return etai.decode(img_bytes)


def _download_or_load_ndjson(url_or_filepath):
    if url_or_filepath.startswith("http"):
        ndjson_bytes = etaw.download_file(url_or_filepath, quiet=True)
        return etas.load_ndjson(ndjson_bytes)

    return etas.read_ndjson(url_or_filepath)
