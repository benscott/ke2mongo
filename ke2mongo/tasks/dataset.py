#!/usr/bin/env python
# encoding: utf-8
"""
Created by 'bens3' on 2013-06-21.
Copyright (c) 2013 'bens3'. All rights reserved.

"""

import os
import luigi
import numpy as np
import pandas as pd
import abc
import ckanapi
import itertools
from collections import OrderedDict
from monary import Monary
from monary.monary import get_monary_numpy_type
from ke2mongo.log import log
from ke2mongo.lib.timeit import timeit
from ke2mongo import config
from ke2mongo.tasks.mongo_catalogue import MongoCatalogueTask
from ke2mongo.tasks.mongo_taxonomy import MongoTaxonomyTask
from ke2mongo.tasks.mongo_multimedia import MongoMultimediaTask
from ke2mongo.tasks.mongo_collection_index import MongoCollectionIndexTask
from ke2mongo.tasks.mongo_site import MongoSiteTask
from ke2mongo.tasks.delete import DeleteTask
from ke2mongo.targets.csv import CSVTarget
from ke2mongo.targets.api import APITarget
from ke2mongo.tasks import MULTIMEDIA_FORMATS, RECORD_STATUS
from ke2mongo.lib.mongo import mongo_client_db, mongo_get_update_markers
from ke2mongo.lib.file import get_export_file_dates

class DatasetTask(luigi.Task):
    """
    Class for processing data mongo into a dataset
    If date set, this task requires all mongo files for that date to have been imported into Mongo DB
    """
    # Parameters
    # Date to process
    date = luigi.IntParameter(default=None)

    # MongoDB params
    collection_name = 'ecatalogue'

    # Should the primary key be prefixed - eg NHMUK:ecatalogue
    primary_key_prefix = 'NHMUK:ecatalogue:'

    # Default record type - used to select records in query
    record_type = None

    has_run = False

    @abc.abstractproperty
    def columns(self):
        """
        Columns to use from mongoDB
        @return: list
        """
        return None

    @abc.abstractmethod
    def output(self):
        """
        Output method
        This overrides luigi.task.output, to ensure it is set
        """
        return None

    @property
    def query(self):
        """
        Query object for selecting data from mongoDB
        @return: dict
        """

        query = OrderedDict()

        if self.record_type:
            query["ColRecordType"] = self.record_type

        query["SecRecordStatus"] = RECORD_STATUS

        if self.date:
            # Ensure we have processed all files for preceding dates
            self.ensure_export_date(self.date)
            query['exportFileDate'] = self.date

        return query

    # CKAN Dataset params
    geospatial_fields = None

    @abc.abstractproperty
    def package(self):
        """
        Package property
        @return: dict
        """
        return None

    @abc.abstractproperty
    def datastore(self):
        """
        Datastore property
        @return: dict
        """
        return None

    @abc.abstractproperty
    def block_size(self):
        """
        Number of records to retrieve
        """
        return None

    def get_primary_key_field(self):
        """
        Return the source primary key fields
        @return:
        """

        for col in self.columns:
            if col[1] == self.datastore['primary_key']:
                return col

    def __init__(self, *args, **kwargs):

        # If a date parameter has been passed in, we'll just use that
        # Otherwise, loop through the files and get all dates
        super(DatasetTask, self).__init__(*args, **kwargs)

        # Create CKAN API instance
        self.ckan = ckanapi.RemoteCKAN(config.get('ckan', 'site_url'), apikey=config.get('ckan', 'api_key'))

        # Get or create the resource object
        self.resource_id = self.get_or_create_resource()

    def ensure_export_date(self, date):
        """
        If cron fails to run for whatever reason, and then reruns the next week, it could be mised
        So when calling this dataset, ensure that all preceding mongo exports have been processed
        @param date: date to check
        @return: None
        """

        def filter_dates(d):
            return d < date

        # Get a list of export files dates and marker dates, prior to the current date being processed
        export_file_dates = filter(filter_dates, get_export_file_dates())
        update_marker_dates = filter(filter_dates, mongo_get_update_markers().keys())

        assert export_file_dates == update_marker_dates, 'Outstanding previous export file dates need to be processed first: %s' % list(set(export_file_dates) - set(update_marker_dates))

    def requires(self):
        # DeleteTask depends upon all other mongo tasks

        # Only require mongo tasks if data parameter is passed in - allows us to rerun for testing
        if self.date:
            yield DeleteTask(self.date)

    def get_or_create_resource(self):
        """

        Either load a resource object
        Or if it doesn't exist, create the dataset package, and datastore

        @param package: params to create the package
        @param datastore: params to create the datastore
        @return: CKAN resource ID
        """

        resource_id = None

        try:
            # If the package exists, retrieve the resource
            ckan_package = self.ckan.action.package_show(id=self.package['name'])

            # Does a resource of the same name already exist for this dataset?
            # If it does, assign to resource_id
            for resource in ckan_package['resources']:
                if resource['name'] == self.datastore['resource']['name']:
                    self.validate_resource(resource)
                    resource_id = resource['id']

        except ckanapi.NotFound:
            log.info("Package %s not found - creating", self.package['name'])

            # Create the package
            ckan_package = self.ckan.action.package_create(**self.package)

        # If we don't have the resource ID, create
        if not resource_id:
            log.info("Resource %s not found - creating", self.datastore['resource']['name'])

            self.datastore['fields'] = [{'id': col, 'type': self.numpy_to_ckan_type(np_type)} for col, np_type in self.get_output_columns().iteritems()]
            self.datastore['resource']['package_id'] = ckan_package['id']

            # API call to create the datastore
            resource_id = self.ckan.action.datastore_create(**self.datastore)['resource_id']

            # If this has geospatial fields, create geom columns
            if self.geospatial_fields:
                log.info("Creating geometry columns for %s", resource_id)
                self.geospatial_fields['resource_id'] = resource_id
                self.ckan.action.create_geom_columns(**self.geospatial_fields)

            log.info("Created datastore resource %s", resource_id)

        return resource_id

    def validate_resource(self, resource):
        # Validate the resource - see DatasetCSVTask
        # Raise Exception on failure
        pass  # default impl

    @staticmethod
    def numpy_to_ckan_type(pandas_type):
        """
        For a pandas field type, return s the corresponding ckan data type, to be used when creating datastore
        init32 => integer
        @param pandas_type: pandas data type
        @return: ckan data type
        """
        try:
            type_num, type_arg, numpy_type = get_monary_numpy_type(pandas_type)
        except ValueError:
            # There is no numpy type - just use original value (JSON)
            return pandas_type;

        try:
            if issubclass(numpy_type, np.signedinteger):
                ckan_type = 'integer'
            elif issubclass(numpy_type, np.floating):
                ckan_type = 'float'
            elif numpy_type is bool:
                ckan_type = 'bool'
            else:
                ckan_type = 'citext'
        except TypeError:
            # Strings are not objects, so we'll get a TypeError
            ckan_type = 'citext'

        return ckan_type


    def get_collection_columns(self, collection=None):
        """
        Parse columns into dictionary keyed by collection name
        And return all fields for a particular collection
        @param collection:
        @return: list of fields
        """
        collection_columns = {}

        for (source_field, destination_field, field_type) in self.columns:
            field_collection, field_name = source_field.split('.')

            try:
                collection_columns[field_collection].append((field_name, destination_field, field_type))
            except KeyError:
                collection_columns[field_collection] = [(field_name, destination_field, field_type)]

        if collection:
            return collection_columns[collection]
        else:
            return collection_columns

    @timeit
    def run(self):

        count = 0

        host = config.get('mongo', 'host')
        db = config.get('mongo', 'database')

        def _fill_field(field_arr, field_type):
            if field_type.startswith('string'):
                field_arr = field_arr.astype(np.str).filled('')
            elif field_type == 'bool':
                field_arr = field_arr.astype(np.str).filled(None)
            elif field_type.startswith('int'):
                field_arr = field_arr.filled(0)
            elif field_type.startswith('float'):
                field_arr = field_arr.filled(np.NaN)
            else:
                raise Exception('Unknown field type %s' % field_type)

            return field_arr

        with Monary(host) as m:

            log.info("Querying Monary")

            # Get field definitions for default collection
            query_fields, df_cols, field_types = zip(*self.get_collection_columns(self.collection_name))

            catalogue_blocks = m.block_query(db, self.collection_name, self.query, query_fields, field_types, block_size=self.block_size)

            log.info("Processing Monary data")

            for catalogue_block in catalogue_blocks:

                # Bit of a hack: fill fields with a blank value (depending on type)
                # So the masked value doesn't get used.  As the masked is shared between
                # each block, if a field is empty it is getting populated by previous values
                catalogue_block = [_fill_field(arr, field_types[i]) for i, arr in enumerate(catalogue_block)]

                # Create a pandas data frame with block of records
                # Columns use the name from the output columns - but must be in the same order as query_fields
                # Which is why we're using tuples for the columns
                df = pd.DataFrame(np.matrix(catalogue_block).transpose(), columns=df_cols)

                # Loop through all the columns and ensure hidden integer fields are cast as int32
                # For example, taxonomy_irn is used to join with taxonomy df
                for i, df_col in enumerate(df_cols):
                    if field_types[i].startswith('int'):
                        df[df_col] = df[df_col].astype(field_types[i])

                df = self.process_dataframe(m, df)

                # Output the dataframe
                self.output().write(df)

                row_count, col_count = df.shape
                count += row_count
                log.info("\t %s records", count)

        # After running, set the has run_flag
        self.has_run = True

    def process_dataframe(self, m, df):

        if self.primary_key_prefix:
            primary_key = self.datastore['primary_key']
            df[primary_key] = self.primary_key_prefix + df[primary_key]

        return df

    @staticmethod
    def _get_unique_irns(df, field_name):
        """
        Return a list of IRNs converted to integers, and not 0 ('0' as treated like string)
        @param df:
        @param field_name:
        @return:
        """
        return pd.unique(df[field_name][df[field_name] != 0].astype('int32').values.ravel()).tolist()

    def ensure_multimedia(self, df, multimedia_field):

        mongo_client = mongo_client_db()

        # The multimedia field contains IRNS of all items - not just images
        # So we need to look up the IRNs against the multimedia record to get the mime type
        # And filter out non-image mimetypes we do not support

        # Convert associatedMedia field to a list
        df[multimedia_field] = df[multimedia_field].apply(lambda x: list(int(z.strip()) for z in x.split(';') if z.strip()))

        def get_max_dimension(str_dimension):
            """
            Split the dimension string, and return the second highest value
            """
            dimensions = [int(x.strip()) for x in str_dimension.split(';')]
            return sorted(dimensions,reverse=True)[1]

        # Get a unique list of IRNS
        unique_multimedia_irns = list(set(itertools.chain(*[irn for irn in df[multimedia_field].values])))

        # Get a list of dictionary of valid multimedia valid mimetypes
        # It's not enough to just check for the derived image heights - some of these are tiffs etc., and undeliverable
        cursor = mongo_client['emultimedia'].find(
            {
                '_id': {'$in': unique_multimedia_irns},
                'MulMimeFormat': {'$in': MULTIMEDIA_FORMATS},
                'DocHeight': {'$exists': True},
                'DocWidth': {'$exists': True}
            },
            {'DocHeight': 1, 'DocWidth': 1}
        )

        multimedia = {
            r['_id']: 'http://www.nhm.ac.uk/emu-classes/class.EMuMedia.php?irn={_id}&image=yes&width={width}&height={height}'.format(
                _id=r['_id'],
                width=get_max_dimension(r['DocWidth']),
                height=get_max_dimension(r['DocHeight'])
            ) for r in cursor
        }

        # And finally update the associatedMedia field, so formatting with the IRN with MULTIMEDIA_URL, if the IRN is in valid_multimedia
        df[multimedia_field] = df[multimedia_field].apply(lambda irns: '; '.join(multimedia[irn] for irn in irns if irn in multimedia))

    @staticmethod
    def get_dataframe(m, collection, columns, irns, key):

        query_fields, df_cols, field_types = zip(*columns)
        assert key in df_cols, 'Merge dataframe key must be present in dataframe columns'

        q = {'_id': {'$in': irns}}

        query = m.query('keemu', collection, q, query_fields, field_types)
        df = pd.DataFrame(np.matrix(query).transpose(), columns=df_cols)

        # Convert to int
        df[key] = df[key].astype('int32')
        # And make index
        df.index = df[key]

        return df

    @staticmethod
    def _is_output_field(field):
        """
        Fields starting with _ are hidden and shouldn't be included in output
        @param field:
        @return: bool
        """
        return not field.startswith('_')

    def get_output_columns(self):

        return OrderedDict((col[1], col[2]) for col in self.columns if self._is_output_field(col[1]))

    def complete(self):
        """
        Built in luigi function - has this task completed
        We want to run this task every time it is called - but if used in a requires statement (cron task)
        It needs to return a complete boolean - so set has_run flag after this task has run
        """
        return self.has_run

class DatasetAPITask(DatasetTask):
    """
    Write directly to CKAN API
    """
    block_size = 200   # 5000 is fastest, but Apache throws 413 “Request Entity Too Large” error

    def output(self):
        return APITarget(resource_id=self.resource_id, columns=self.get_output_columns())

class DatasetCSVTask(DatasetTask):
    """
    Output dataset to CSV
    """

    # >100 causes process to hang when looking up lots of part/parents (which are grouped). Otherwise 2500 is optimum
    block_size = 100

    @property
    def path(self):
        """
        File name to output
        @return: str
        """
        file_name = self.__class__.__name__.replace('DatasetCSVTask', '').lower()

        if self.date:
            file_name += '-' + str(self.date)

        return os.path.join(config.get('csv', 'output_dir'), file_name + '.csv')

    def output(self):
        """
        Luigi method: output target
        @return: luigi file ref
        """
        return CSVTarget(path=self.path, columns=self.get_output_columns())

    def validate_resource(self, resource):
        """
        Validate the resource
        Make sure the fields we're creating in the CSV are the same as in the dataset
        @param resource: resource dict
        @return: None
        """

        # Load the datastore fields (limit = 0 so no rows returned)
        datastore = self.ckan.action.datastore_search(resource_id=resource['id'], limit=0)

        # Create a list of all (non-internal - _id) datastore fields we'd expect in the CSV
        datastore_fields = [field['id'] for field in datastore['fields'] if field['id'] != '_id']
        columns = [col for col in self.get_output_columns().keys()]

        assert datastore_fields == columns, 'Current datastore fields do not match CSV fields'

    def on_success(self):

        log.info("Import CSV file with:")
        log.info("COPY \"{resource_id}\" (\"{cols}\") FROM '{path}' DELIMITER ',' CSV ENCODING 'UTF8';".format(
            resource_id=self.resource_id,
            cols='","'.join(col for col in self.get_output_columns()),
            path=self.path
        ))

        log.info("And update full text index:")
        log.info("paster update-fulltext -i \"{resource_id}\" -c /vagrant/etc/default/development.ini".format(
            resource_id=self.resource_id,
        ))

        return super(DatasetCSVTask, self).complete()