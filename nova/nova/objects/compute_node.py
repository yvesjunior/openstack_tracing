#    Copyright 2013 IBM Corp
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

from oslo_config import cfg
from oslo_serialization import jsonutils
import six

from nova import db
from nova import exception
from nova import objects
from nova.objects import base
from nova.objects import fields
from nova.objects import pci_device_pool
from nova import utils

CONF = cfg.CONF
CONF.import_opt('cpu_allocation_ratio', 'nova.compute.resource_tracker')
CONF.import_opt('ram_allocation_ratio', 'nova.compute.resource_tracker')


# TODO(berrange): Remove NovaObjectDictCompat
@base.NovaObjectRegistry.register
class ComputeNode(base.NovaPersistentObject, base.NovaObject,
                  base.NovaObjectDictCompat):
    # Version 1.0: Initial version
    # Version 1.1: Added get_by_service_id()
    # Version 1.2: String attributes updated to support unicode
    # Version 1.3: Added stats field
    # Version 1.4: Added host ip field
    # Version 1.5: Added numa_topology field
    # Version 1.6: Added supported_hv_specs
    # Version 1.7: Added host field
    # Version 1.8: Added get_by_host_and_nodename()
    # Version 1.9: Added pci_device_pools
    # Version 1.10: Added get_first_node_by_host_for_old_compat()
    # Version 1.11: PciDevicePoolList version 1.1
    # Version 1.12: HVSpec version 1.1
    # Version 1.13: Changed service_id field to be nullable
    # Version 1.14: Added cpu_allocation_ratio and ram_allocation_ratio
    VERSION = '1.14'

    fields = {
        'id': fields.IntegerField(read_only=True),
        'service_id': fields.IntegerField(nullable=True),
        'host': fields.StringField(nullable=True),
        'vcpus': fields.IntegerField(),
        'memory_mb': fields.IntegerField(),
        'local_gb': fields.IntegerField(),
        'vcpus_used': fields.IntegerField(),
        'memory_mb_used': fields.IntegerField(),
        'local_gb_used': fields.IntegerField(),
        'hypervisor_type': fields.StringField(),
        'hypervisor_version': fields.IntegerField(),
        'hypervisor_hostname': fields.StringField(nullable=True),
        'free_ram_mb': fields.IntegerField(nullable=True),
        'free_disk_gb': fields.IntegerField(nullable=True),
        'current_workload': fields.IntegerField(nullable=True),
        'running_vms': fields.IntegerField(nullable=True),
        'cpu_info': fields.StringField(nullable=True),
        'disk_available_least': fields.IntegerField(nullable=True),
        'metrics': fields.StringField(nullable=True),
        'stats': fields.DictOfNullableStringsField(nullable=True),
        'host_ip': fields.IPAddressField(nullable=True),
        'numa_topology': fields.StringField(nullable=True),
        # NOTE(pmurray): the supported_hv_specs field maps to the
        # supported_instances field in the database
        'supported_hv_specs': fields.ListOfObjectsField('HVSpec'),
        # NOTE(pmurray): the pci_device_pools field maps to the
        # pci_stats field in the database
        'pci_device_pools': fields.ObjectField('PciDevicePoolList',
                                               nullable=True),
        'cpu_allocation_ratio': fields.FloatField(),
        'ram_allocation_ratio': fields.FloatField(),
        }

    obj_relationships = {
        'pci_device_pools': [('1.9', '1.0'), ('1.11', '1.1')],
        'supported_hv_specs': [('1.6', '1.0'), ('1.12', '1.1')],
    }

    def obj_make_compatible(self, primitive, target_version):
        super(ComputeNode, self).obj_make_compatible(primitive, target_version)
        target_version = utils.convert_version_to_tuple(target_version)
        if target_version < (1, 14):
            if 'ram_allocation_ratio' in primitive:
                del primitive['ram_allocation_ratio']
            if 'cpu_allocation_ratio' in primitive:
                del primitive['cpu_allocation_ratio']
        if target_version < (1, 13) and primitive.get('service_id') is None:
            # service_id is non-nullable in versions before 1.13
            try:
                service = objects.Service.get_by_compute_host(
                    self._context, primitive['host'])
                primitive['service_id'] = service.id
            except (exception.ComputeHostNotFound, KeyError):
                # NOTE(hanlind): In case anything goes wrong like service not
                # found or host not being set, catch and set a fake value just
                # to allow for older versions that demand a value to work.
                # Setting to -1 will, if value is later used result in a
                # ServiceNotFound, so should be safe.
                primitive['service_id'] = -1
        if target_version < (1, 7) and 'host' in primitive:
            del primitive['host']
        if target_version < (1, 5) and 'numa_topology' in primitive:
            del primitive['numa_topology']
        if target_version < (1, 4) and 'host_ip' in primitive:
            del primitive['host_ip']
        if target_version < (1, 3) and 'stats' in primitive:
            # pre 1.3 version does not have a stats field
            del primitive['stats']

    @staticmethod
    def _host_from_db_object(compute, db_compute):
        if (('host' not in db_compute or db_compute['host'] is None)
                and 'service_id' in db_compute
                and db_compute['service_id'] is not None):
            # FIXME(sbauza) : Unconverted compute record, provide compatibility
            # This has to stay until we can be sure that any/all compute nodes
            # in the database have been converted to use the host field

            # Service field of ComputeNode could be deprecated in a next patch,
            # so let's use directly the Service object
            try:
                service = objects.Service.get_by_id(
                    compute._context, db_compute['service_id'])
            except exception.ServiceNotFound:
                compute['host'] = None
                return
            try:
                compute['host'] = service.host
            except (AttributeError, exception.OrphanedObjectError):
                # Host can be nullable in Service
                compute['host'] = None
        elif 'host' in db_compute and db_compute['host'] is not None:
            # New-style DB having host as a field
            compute['host'] = db_compute['host']
        else:
            # We assume it should not happen but in case, let's set it to None
            compute['host'] = None

    @staticmethod
    def _from_db_object(context, compute, db_compute):
        special_cases = set([
            'stats',
            'supported_hv_specs',
            'host',
            'pci_device_pools',
            ])
        fields = set(compute.fields) - special_cases
        for key in fields:
            value = db_compute[key]
            # NOTE(sbauza): Since all compute nodes don't possibly run the
            # latest RT code updating allocation ratios, we need to provide
            # a backwards compatible way of hydrating them.
            # As we want to care about our operators and since we don't want to
            # ask them to change their configuration files before upgrading, we
            # prefer to hardcode the default values for the ratios here until
            # the next release (Mitaka) where the opt default values will be
            # restored for both cpu (16.0) and ram (1.5) allocation ratios.
            # TODO(sbauza): Remove that in the next major version bump where
            # we break compatibilility with old Kilo computes
            if key == 'cpu_allocation_ratio' or key == 'ram_allocation_ratio':
                if value == 0.0:
                    # Operator has not yet provided a new value for that ratio
                    # on the compute node
                    value = None
                if value is None:
                    # ResourceTracker is not updating the value (old node)
                    # or the compute node is updated but the default value has
                    # not been changed
                    value = getattr(CONF, key)
                    if value == 0.0 and key == 'cpu_allocation_ratio':
                        # It's not specified either on the controller
                        value = 16.0
                    if value == 0.0 and key == 'ram_allocation_ratio':
                        # It's not specified either on the controller
                        value = 1.5
            compute[key] = value

        stats = db_compute['stats']
        if stats:
            compute['stats'] = jsonutils.loads(stats)

        sup_insts = db_compute.get('supported_instances')
        if sup_insts:
            hv_specs = jsonutils.loads(sup_insts)
            hv_specs = [objects.HVSpec.from_list(hv_spec)
                        for hv_spec in hv_specs]
            compute['supported_hv_specs'] = hv_specs

        pci_stats = db_compute.get('pci_stats')
        compute.pci_device_pools = pci_device_pool.from_pci_stats(pci_stats)
        compute._context = context

        # Make sure that we correctly set the host field depending on either
        # host column is present in the table or not
        compute._host_from_db_object(compute, db_compute)

        compute.obj_reset_changes()
        return compute

    @base.remotable_classmethod
    def get_by_id(cls, context, compute_id):
        db_compute = db.compute_node_get(context, compute_id)
        return cls._from_db_object(context, cls(), db_compute)

    # NOTE(hanlind): This is deprecated and should be removed on the next
    # major version bump
    @base.remotable_classmethod
    def get_by_service_id(cls, context, service_id):
        db_computes = db.compute_nodes_get_by_service_id(context, service_id)
        # NOTE(sbauza): Old version was returning an item, we need to keep this
        # behaviour for backwards compatibility
        db_compute = db_computes[0]
        return cls._from_db_object(context, cls(), db_compute)

    @base.remotable_classmethod
    def get_by_host_and_nodename(cls, context, host, nodename):
        try:
            db_compute = db.compute_node_get_by_host_and_nodename(
                context, host, nodename)
        except exception.ComputeHostNotFound:
            # FIXME(sbauza): Some old computes can still have no host record
            # We need to provide compatibility by using the old service_id
            # record.
            # We assume the compatibility as an extra penalty of one more DB
            # call but that's necessary until all nodes are upgraded.
            try:
                service = objects.Service.get_by_compute_host(context, host)
                db_computes = db.compute_nodes_get_by_service_id(
                    context, service.id)
            except exception.ServiceNotFound:
                # We need to provide the same exception upstream
                raise exception.ComputeHostNotFound(host=host)
            db_compute = None
            for compute in db_computes:
                if compute['hypervisor_hostname'] == nodename:
                    db_compute = compute
                    # We can avoid an extra call to Service object in
                    # _from_db_object
                    db_compute['host'] = service.host
                    break
            if not db_compute:
                raise exception.ComputeHostNotFound(host=host)
        return cls._from_db_object(context, cls(), db_compute)

    @base.remotable_classmethod
    def get_first_node_by_host_for_old_compat(cls, context, host,
                                              use_subordinate=False):
        computes = ComputeNodeList.get_all_by_host(context, host, use_subordinate)
        # FIXME(sbauza): Some hypervisors (VMware, Ironic) can return multiple
        # nodes per host, we should return all the nodes and modify the callers
        # instead.
        # Arbitrarily returning the first node.
        return computes[0]

    @staticmethod
    def _convert_stats_to_db_format(updates):
        stats = updates.pop('stats', None)
        if stats is not None:
            updates['stats'] = jsonutils.dumps(stats)

    @staticmethod
    def _convert_host_ip_to_db_format(updates):
        host_ip = updates.pop('host_ip', None)
        if host_ip:
            updates['host_ip'] = str(host_ip)

    @staticmethod
    def _convert_supported_instances_to_db_format(updates):
        hv_specs = updates.pop('supported_hv_specs', None)
        if hv_specs is not None:
            hv_specs = [hv_spec.to_list() for hv_spec in hv_specs]
            updates['supported_instances'] = jsonutils.dumps(hv_specs)

    @staticmethod
    def _convert_pci_stats_to_db_format(updates):
        pools = updates.pop('pci_device_pools', None)
        if pools:
            updates['pci_stats'] = jsonutils.dumps(pools.obj_to_primitive())

    @base.remotable
    def create(self):
        if self.obj_attr_is_set('id'):
            raise exception.ObjectActionError(action='create',
                                              reason='already created')
        updates = self.obj_get_changes()
        self._convert_stats_to_db_format(updates)
        self._convert_host_ip_to_db_format(updates)
        self._convert_supported_instances_to_db_format(updates)
        self._convert_pci_stats_to_db_format(updates)

        db_compute = db.compute_node_create(self._context, updates)
        self._from_db_object(self._context, self, db_compute)

    @base.remotable
    def save(self, prune_stats=False):
        # NOTE(belliott) ignore prune_stats param, no longer relevant

        updates = self.obj_get_changes()
        updates.pop('id', None)
        self._convert_stats_to_db_format(updates)
        self._convert_host_ip_to_db_format(updates)
        self._convert_supported_instances_to_db_format(updates)
        self._convert_pci_stats_to_db_format(updates)

        db_compute = db.compute_node_update(self._context, self.id, updates)
        self._from_db_object(self._context, self, db_compute)

    @base.remotable
    def destroy(self):
        db.compute_node_delete(self._context, self.id)

    def update_from_virt_driver(self, resources):
        # NOTE(pmurray): the virt driver provides a dict of values that
        # can be copied into the compute node. The names and representation
        # do not exactly match.
        # TODO(pmurray): the resources dict should be formalized.
        keys = ["vcpus", "memory_mb", "local_gb", "cpu_info",
                "vcpus_used", "memory_mb_used", "local_gb_used",
                "numa_topology", "hypervisor_type",
                "hypervisor_version", "hypervisor_hostname",
                "disk_available_least", "host_ip"]
        for key in keys:
            if key in resources:
                self[key] = resources[key]

        # supported_instances has a different name in compute_node
        # TODO(pmurray): change virt drivers not to json encode
        # values they add to the resources dict
        if 'supported_instances' in resources:
            si = resources['supported_instances']
            if isinstance(si, six.string_types):
                si = jsonutils.loads(si)
            self.supported_hv_specs = [objects.HVSpec.from_list(s) for s in si]


@base.NovaObjectRegistry.register
class ComputeNodeList(base.ObjectListBase, base.NovaObject):
    # Version 1.0: Initial version
    #              ComputeNode <= version 1.2
    # Version 1.1 ComputeNode version 1.3
    # Version 1.2 Add get_by_service()
    # Version 1.3 ComputeNode version 1.4
    # Version 1.4 ComputeNode version 1.5
    # Version 1.5 Add use_subordinate to get_by_service
    # Version 1.6 ComputeNode version 1.6
    # Version 1.7 ComputeNode version 1.7
    # Version 1.8 ComputeNode version 1.8 + add get_all_by_host()
    # Version 1.9 ComputeNode version 1.9
    # Version 1.10 ComputeNode version 1.10
    # Version 1.11 ComputeNode version 1.11
    # Version 1.12 ComputeNode version 1.12
    # Version 1.13 ComputeNode version 1.13
    # Version 1.14 ComputeNode version 1.14
    VERSION = '1.14'
    fields = {
        'objects': fields.ListOfObjectsField('ComputeNode'),
        }
    # NOTE(danms): ComputeNode was at 1.2 before we added this
    obj_relationships = {
        'objects': [('1.0', '1.2'), ('1.1', '1.3'), ('1.2', '1.3'),
                    ('1.3', '1.4'), ('1.4', '1.5'), ('1.5', '1.5'),
                    ('1.6', '1.6'), ('1.7', '1.7'), ('1.8', '1.8'),
                    ('1.9', '1.9'), ('1.10', '1.10'), ('1.11', '1.11'),
                    ('1.12', '1.12'), ('1.13', '1.13'), ('1.14', '1.14')],
        }

    @base.remotable_classmethod
    def get_all(cls, context):
        db_computes = db.compute_node_get_all(context)
        return base.obj_make_list(context, cls(context), objects.ComputeNode,
                                  db_computes)

    @base.remotable_classmethod
    def get_by_hypervisor(cls, context, hypervisor_match):
        db_computes = db.compute_node_search_by_hypervisor(context,
                                                           hypervisor_match)
        return base.obj_make_list(context, cls(context), objects.ComputeNode,
                                  db_computes)

    # NOTE(hanlind): This is deprecated and should be removed on the next
    # major version bump
    @base.remotable_classmethod
    def _get_by_service(cls, context, service_id, use_subordinate=False):
        try:
            db_computes = db.compute_nodes_get_by_service_id(
                context, service_id)
        except exception.ServiceNotFound:
            # NOTE(sbauza): Previous behaviour was returning an empty list
            # if the service was created with no computes, we need to keep it.
            db_computes = []
        return base.obj_make_list(context, cls(context), objects.ComputeNode,
                                  db_computes)

    @base.remotable_classmethod
    def get_all_by_host(cls, context, host, use_subordinate=False):
        try:
            db_computes = db.compute_node_get_all_by_host(context, host,
                                                          use_subordinate)
        except exception.ComputeHostNotFound:
            # FIXME(sbauza): Some old computes can still have no host record
            # We need to provide compatibility by using the old service_id
            # record.
            # We assume the compatibility as an extra penalty of one more DB
            # call but that's necessary until all nodes are upgraded.
            try:
                service = objects.Service.get_by_compute_host(context, host,
                                                              use_subordinate)
                db_computes = db.compute_nodes_get_by_service_id(
                    context, service.id)
            except exception.ServiceNotFound:
                # We need to provide the same exception upstream
                raise exception.ComputeHostNotFound(host=host)
            # We can avoid an extra call to Service object in _from_db_object
            for db_compute in db_computes:
                db_compute['host'] = service.host
        return base.obj_make_list(context, cls(context), objects.ComputeNode,
                                  db_computes)
