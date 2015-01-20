#!/usr/bin/env python
# encoding: utf-8
"""
Created by 'bens3' on 2013-06-21.
Copyright (c) 2013 'bens3'. All rights reserved.
"""

import luigi
import ckanapi
from keparser import KEParser
from ke2mongo import config
from ke2mongo.log import log
from ke2mongo.lib.timeit import timeit
from ke2mongo.tasks.mongo import MongoTask
from ke2mongo.targets.ke import KEFileTarget

# Need all mongo tasks, as we dynamically retrieve the collections
from ke2mongo.tasks.mongo_catalogue import MongoCatalogueTask
from ke2mongo.tasks.mongo_taxonomy import MongoTaxonomyTask
from ke2mongo.tasks.mongo_multimedia import MongoMultimediaTask
from ke2mongo.tasks.mongo_collection_index import MongoCollectionIndexTask
from ke2mongo.tasks.mongo_collection_event import MongoCollectionEventTask
from ke2mongo.tasks.mongo_site import MongoSiteTask


class DeleteTask(MongoTask):
    """
    Delete Task for deleting from mongo and
    """

    module = 'eaudit'
    file_extension = 'deleted-export'

    # Set up CKAN API connection
    ckan = ckanapi.RemoteCKAN(config.get('ckan', 'site_url'), apikey=config.get('ckan', 'api_key'))

    def requires(self):

        # For delete to run, all other mongo tasks for same date must have already run
        req = [
            MongoCatalogueTask(self.date),
            MongoTaxonomyTask(self.date),
            MongoMultimediaTask(self.date),
            MongoCollectionIndexTask(self.date),
            MongoCollectionEventTask(self.date),
            MongoSiteTask(self.date),
        ]

        # Full export does not include an eaudit file
        # So we do not want to try processing
        if not self.is_full_export():
            req.append(super(DeleteTask, self).requires())

        return req


    def is_full_export(self):
        """
        Is this data a full export date
        @return:
        """
        return self.date == int(config.get('keemu', 'full_export_date'))

    @timeit
    def run(self):


        # Build a dict of all modules and collections
        # We then retrieve the appropriate collection from the records module name (AudTable)
        collections = {}
        for cls in MongoTask.__subclasses__():
            collections[cls.module] = cls(None).get_collection()

        ke_file_target = None

        # We have multiple requirements (& thus inputs)
        # Loop through to find the ke file target
        for i in self.input():
            if isinstance(i, KEFileTarget):
                ke_file_target = i
                break

        # If we have a KE target file for this delete operation, process it
        # We won't if this data is for a full export
        if ke_file_target:

            ke_data = KEParser(ke_file_target.open('r'), file_path=ke_file_target.path, schema_file=self.keemu_schema_file)

            for record in self.iterate_data(ke_data):

                module = record.get('AudTable')
                irn = record.get('AudKey')
                try:
                    collection = collections[module]
                except KeyError:
                    log.debug('Skipping eaudit record for %s' % module)
                    # We do not have a collection for this module - skip to next record
                    continue
                else:
                    log.info('Deleting record %s(%s)' % (module, irn))

                    # If this is an ecatalogue record, try and delete from CKAN
                    if collection.name == 'ecatalogue':
                        self.ckan_delete(collection, irn)

                    # And then delete from mongoDB
                    collection.remove({'_id': irn})

        self.mark_complete()

    def ckan_delete(self, collection, irn):

        # Load record from MongoDB
        log.info('Load MongoDB record %s' % irn)

        # Load the record from mongo
        mongo_record = collection.find_one({'_id': int(irn)})

        if mongo_record:

            # To avoid circular imports, import the tasks we need to check here
            # Dataset tasks are dependent on the DeleteTask
            from ke2mongo.tasks.indexlot import IndexLotDatasetAPITask
            from ke2mongo.tasks.artefact import ArtefactDatasetAPITask
            from ke2mongo.tasks.specimen import SpecimenDatasetAPITask

            # By default, use SpecimenDatasetAPITask
            task_cls = SpecimenDatasetAPITask

            # Override default class if is Index Lot or Artefact
            for t in [IndexLotDatasetAPITask, ArtefactDatasetAPITask]:
                if t.record_type == mongo_record['ColRecordType']:
                    task_cls = t
                    break

            # Initiate the task class so we can access values and methods
            task = task_cls()
            primary_key_field = task.get_primary_key_field()

            # Get the source primary key - this needs to be split on . as we have added the collection name
            ke_primary_key = primary_key_field[0].split('.')[1]

            # The name of the primary key field used in CKAN
            ckan_primary_key = primary_key_field[1]

            primary_key_value = mongo_record[ke_primary_key]

            # Load the resource, so we can find the resource ID
            resource = self.ckan.action.resource_show(id=task_cls.datastore['resource']['name'])

            # And delete the record from the datastore
            log.info('Deleting record from CKAN where %s=%s' % (ckan_primary_key, primary_key_value))
            self.ckan.action.datastore_delete(id=resource['id'], filters={ckan_primary_key: primary_key_value})

if __name__ == "__main__":
    luigi.run(main_task_cls=DeleteTask)
