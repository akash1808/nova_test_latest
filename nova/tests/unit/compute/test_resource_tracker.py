# Copyright (c) 2012 OpenStack Foundation
# All Rights Reserved.
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

"""Tests for compute resource tracking."""

import copy
import six
import uuid

import mock
from oslo_config import cfg
from oslo_serialization import jsonutils
from oslo_utils import timeutils

from nova.compute.monitors import base as monitor_base
from nova.compute import resource_tracker
from nova.compute import resources
from nova.compute import task_states
from nova.compute import vm_states
from nova import context
from nova import db
from nova import exception
from nova import objects
from nova.objects import base as obj_base
from nova.objects import pci_device_pool
from nova import rpc
from nova import test
from nova.tests.unit.pci import fakes as pci_fakes
from nova.virt import driver


FAKE_VIRT_MEMORY_MB = 5
FAKE_VIRT_MEMORY_OVERHEAD = 1
FAKE_VIRT_MEMORY_WITH_OVERHEAD = (
        FAKE_VIRT_MEMORY_MB + FAKE_VIRT_MEMORY_OVERHEAD)
FAKE_VIRT_NUMA_TOPOLOGY = objects.NUMATopology(
        cells=[objects.NUMACell(id=0, cpuset=set([1, 2]), memory=3072,
                                cpu_usage=0, memory_usage=0, mempages=[],
                                siblings=[], pinned_cpus=set([])),
               objects.NUMACell(id=1, cpuset=set([3, 4]), memory=3072,
                                cpu_usage=0, memory_usage=0, mempages=[],
                                siblings=[], pinned_cpus=set([]))])
FAKE_VIRT_NUMA_TOPOLOGY_OVERHEAD = objects.NUMATopologyLimits(
    cpu_allocation_ratio=2, ram_allocation_ratio=2)

ROOT_GB = 5
EPHEMERAL_GB = 1
FAKE_VIRT_LOCAL_GB = ROOT_GB + EPHEMERAL_GB
FAKE_VIRT_VCPUS = 1
FAKE_VIRT_STATS = {'virt_stat': 10}
FAKE_VIRT_STATS_COERCED = {'virt_stat': '10'}
FAKE_VIRT_STATS_JSON = jsonutils.dumps(FAKE_VIRT_STATS)
RESOURCE_NAMES = ['vcpu']
CONF = cfg.CONF


class UnsupportedVirtDriver(driver.ComputeDriver):
    """Pretend version of a lame virt driver."""

    def __init__(self):
        super(UnsupportedVirtDriver, self).__init__(None)

    def get_host_ip_addr(self):
        return '127.0.0.1'

    def get_available_resource(self, nodename):
        # no support for getting resource usage info
        return {}


class FakeVirtDriver(driver.ComputeDriver):

    def __init__(self, pci_support=False, stats=None,
                 numa_topology=FAKE_VIRT_NUMA_TOPOLOGY):
        super(FakeVirtDriver, self).__init__(None)
        self.memory_mb = FAKE_VIRT_MEMORY_MB
        self.local_gb = FAKE_VIRT_LOCAL_GB
        self.vcpus = FAKE_VIRT_VCPUS
        self.numa_topology = numa_topology

        self.memory_mb_used = 0
        self.local_gb_used = 0
        self.pci_support = pci_support
        self.pci_devices = [
            {
                'label': 'label_8086_0443',
                'dev_type': 'type-VF',
                'compute_node_id': 1,
                'address': '0000:00:01.1',
                'product_id': '0443',
                'vendor_id': '8086',
                'status': 'available',
                'extra_k1': 'v1',
                'numa_node': 1
            },
            {
                'label': 'label_8086_0443',
                'dev_type': 'type-VF',
                'compute_node_id': 1,
                'address': '0000:00:01.2',
                'product_id': '0443',
                'vendor_id': '8086',
                'status': 'available',
                'extra_k1': 'v1',
                'numa_node': 1
            },
            {
                'label': 'label_8086_0443',
                'dev_type': 'type-PF',
                'compute_node_id': 1,
                'address': '0000:00:01.0',
                'product_id': '0443',
                'vendor_id': '8086',
                'status': 'available',
                'extra_k1': 'v1',
                'numa_node': 1
            },
            {
                'label': 'label_8086_0123',
                'dev_type': 'type-PCI',
                'compute_node_id': 1,
                'address': '0000:00:01.0',
                'product_id': '0123',
                'vendor_id': '8086',
                'status': 'available',
                'extra_k1': 'v1',
                'numa_node': 1
            },
            {
                'label': 'label_8086_7891',
                'dev_type': 'type-VF',
                'compute_node_id': 1,
                'address': '0000:00:01.0',
                'product_id': '7891',
                'vendor_id': '8086',
                'status': 'available',
                'extra_k1': 'v1',
                'numa_node': None
            },
        ] if self.pci_support else []
        self.pci_stats = [
            {
                'count': 2,
                'vendor_id': '8086',
                'product_id': '0443',
                'numa_node': 1
            },
            {
                'count': 1,
                'vendor_id': '8086',
                'product_id': '7891',
                'numa_node': None
            },
        ] if self.pci_support else []
        if stats is not None:
            self.stats = stats

    def get_host_ip_addr(self):
        return '127.0.0.1'

    def get_available_resource(self, nodename):
        d = {
            'vcpus': self.vcpus,
            'memory_mb': self.memory_mb,
            'local_gb': self.local_gb,
            'vcpus_used': 0,
            'memory_mb_used': self.memory_mb_used,
            'local_gb_used': self.local_gb_used,
            'hypervisor_type': 'fake',
            'hypervisor_version': 0,
            'hypervisor_hostname': 'fakehost',
            'cpu_info': '',
            'numa_topology': (
                self.numa_topology._to_json() if self.numa_topology else None),
        }
        if self.pci_support:
            d['pci_passthrough_devices'] = jsonutils.dumps(self.pci_devices)
        if hasattr(self, 'stats'):
            d['stats'] = self.stats
        return d

    def estimate_instance_overhead(self, instance_info):
        instance_info['memory_mb']  # make sure memory value is present
        overhead = {
            'memory_mb': FAKE_VIRT_MEMORY_OVERHEAD
        }
        return overhead  # just return a constant value for testing


class BaseTestCase(test.TestCase):

    @mock.patch('stevedore.enabled.EnabledExtensionManager')
    def setUp(self, _mock_ext_mgr):
        super(BaseTestCase, self).setUp()

        self.flags(reserved_host_disk_mb=0,
                   reserved_host_memory_mb=0)

        self.context = context.get_admin_context()

        self.flags(pci_passthrough_whitelist=[
            '{"vendor_id": "8086", "product_id": "0443"}',
            '{"vendor_id": "8086", "product_id": "7891"}'])
        self.flags(use_local=True, group='conductor')
        self.conductor = self.start_service('conductor',
                                            manager=CONF.conductor.manager)

        self._instances = {}
        self._instance_types = {}

        self.stubs.Set(objects.InstanceList, 'get_by_host_and_node',
                       self._fake_instance_get_by_host_and_node)
        self.stubs.Set(self.conductor.db,
                       'flavor_get', self._fake_flavor_get)

        self.host = 'fakehost'
        self.compute = self._create_compute_node()
        self.updated = False
        self.deleted = False
        self.update_call_count = 0

    def _create_compute_node(self, values=None):
        # This creates a db representation of a compute_node.
        compute = {
            "id": 1,
            "service_id": 1,
            "host": "fakehost",
            "vcpus": 1,
            "memory_mb": 1,
            "local_gb": 1,
            "vcpus_used": 1,
            "memory_mb_used": 1,
            "local_gb_used": 1,
            "free_ram_mb": 1,
            "free_disk_gb": 1,
            "current_workload": 1,
            "running_vms": 0,
            "cpu_info": None,
            "numa_topology": None,
            "stats": '{"num_instances": "1"}',
            "hypervisor_hostname": "fakenode",
            'hypervisor_version': 1,
            'hypervisor_type': 'fake-hyp',
            'disk_available_least': None,
            'host_ip': None,
            'metrics': None,
            'created_at': None,
            'updated_at': None,
            'deleted_at': None,
            'deleted': False,
        }
        if values:
            compute.update(values)
        return compute

    def _create_compute_node_obj(self, context):
        # Use the db representation of a compute node returned
        # by _create_compute_node() to create an equivalent compute
        # node object.
        compute = self._create_compute_node()
        compute_obj = objects.ComputeNode()
        compute_obj = objects.ComputeNode._from_db_object(
            context, compute_obj, compute)
        return compute_obj

    def _create_service(self, host="fakehost", compute=None):
        if compute:
            compute = [compute]

        service = {
            "id": 1,
            "host": host,
            "binary": "nova-compute",
            "topic": "compute",
            "compute_node": compute,
            "report_count": 0,
            'disabled': False,
            'disabled_reason': None,
            'created_at': None,
            'updated_at': None,
            'deleted_at': None,
            'deleted': False,
            'last_seen_up': None,
            'forced_down': False
        }
        return service

    def _fake_instance_obj(self, stash=True, flavor=None, **kwargs):

        # Default to an instance ready to resize to or from the same
        # instance_type
        flavor = flavor or self._fake_flavor_create()
        if not isinstance(flavor, objects.Flavor):
            flavor = objects.Flavor(**flavor)

        instance_uuid = str(uuid.uuid1())
        instance = objects.Instance(context=self.context, uuid=instance_uuid,
                                    flavor=flavor)
        instance.update({
            'vm_state': vm_states.RESIZED,
            'task_state': None,
            'ephemeral_key_uuid': None,
            'os_type': 'Linux',
            'project_id': '123456',
            'host': None,
            'node': None,
            'instance_type_id': flavor['id'],
            'memory_mb': flavor['memory_mb'],
            'vcpus': flavor['vcpus'],
            'root_gb': flavor['root_gb'],
            'ephemeral_gb': flavor['ephemeral_gb'],
            'launched_on': None,
            'system_metadata': {},
            'availability_zone': None,
            'vm_mode': None,
            'reservation_id': None,
            'display_name': None,
            'default_swap_device': None,
            'power_state': None,
            'access_ip_v6': None,
            'access_ip_v4': None,
            'key_name': None,
            'updated_at': None,
            'cell_name': None,
            'locked': None,
            'locked_by': None,
            'launch_index': None,
            'architecture': None,
            'auto_disk_config': None,
            'terminated_at': None,
            'ramdisk_id': None,
            'user_data': None,
            'cleaned': None,
            'deleted_at': None,
            'id': 333,
            'disable_terminate': None,
            'hostname': None,
            'display_description': None,
            'key_data': None,
            'deleted': None,
            'default_ephemeral_device': None,
            'progress': None,
            'launched_at': None,
            'config_drive': None,
            'kernel_id': None,
            'user_id': None,
            'shutdown_terminate': None,
            'created_at': None,
            'image_ref': None,
            'root_device_name': None,
        })

        if stash:
            instance.old_flavor = flavor
            instance.new_flavor = flavor

        instance.numa_topology = kwargs.pop('numa_topology', None)

        instance.update(kwargs)

        self._instances[instance_uuid] = instance
        return instance

    def _fake_flavor_create(self, **kwargs):
        instance_type = {
            'id': 1,
            'created_at': None,
            'updated_at': None,
            'deleted_at': None,
            'deleted': False,
            'disabled': False,
            'is_public': True,
            'name': 'fakeitype',
            'memory_mb': FAKE_VIRT_MEMORY_MB,
            'vcpus': FAKE_VIRT_VCPUS,
            'root_gb': ROOT_GB,
            'ephemeral_gb': EPHEMERAL_GB,
            'swap': 0,
            'rxtx_factor': 1.0,
            'vcpu_weight': 1,
            'flavorid': 'fakeflavor',
            'extra_specs': {},
        }
        instance_type.update(**kwargs)
        instance_type = objects.Flavor(**instance_type)

        id_ = instance_type['id']
        self._instance_types[id_] = instance_type
        return instance_type

    def _fake_instance_get_by_host_and_node(self, context, host, nodename,
                                            expected_attrs=None):
        return objects.InstanceList(
            objects=[i for i in self._instances.values() if i['host'] == host])

    def _fake_flavor_get(self, ctxt, id_):
        return self._instance_types[id_]

    def _fake_compute_node_update(self, ctx, compute_node_id, values,
            prune_stats=False):
        self.update_call_count += 1
        self.updated = True
        self.compute.update(values)
        return self.compute

    def _driver(self):
        return FakeVirtDriver()

    def _tracker(self, host=None):

        if host is None:
            host = self.host

        node = "fakenode"

        driver = self._driver()

        tracker = resource_tracker.ResourceTracker(host, driver, node)
        tracker.compute_node = self._create_compute_node_obj(self.context)
        tracker.ext_resources_handler = \
            resources.ResourceHandler(RESOURCE_NAMES, True)
        return tracker


class UnsupportedDriverTestCase(BaseTestCase):
    """Resource tracking should be disabled when the virt driver doesn't
    support it.
    """
    def setUp(self):
        super(UnsupportedDriverTestCase, self).setUp()
        self.tracker = self._tracker()
        # seed tracker with data:
        self.tracker.update_available_resource(self.context)

    def _driver(self):
        return UnsupportedVirtDriver()

    def test_disabled(self):
        # disabled = no compute node stats
        self.assertTrue(self.tracker.disabled)
        self.assertIsNone(self.tracker.compute_node)

    def test_disabled_claim(self):
        # basic claim:
        instance = self._fake_instance_obj()
        with mock.patch.object(instance, 'save'):
            claim = self.tracker.instance_claim(self.context, instance)
        self.assertEqual(0, claim.memory_mb)

    def test_disabled_instance_claim(self):
        # instance variation:
        instance = self._fake_instance_obj()
        with mock.patch.object(instance, 'save'):
            claim = self.tracker.instance_claim(self.context, instance)
        self.assertEqual(0, claim.memory_mb)

    @mock.patch('nova.objects.Instance.save')
    def test_disabled_instance_context_claim(self, mock_save):
        # instance context manager variation:
        instance = self._fake_instance_obj()
        self.tracker.instance_claim(self.context, instance)
        with self.tracker.instance_claim(self.context, instance) as claim:
            self.assertEqual(0, claim.memory_mb)

    def test_disabled_updated_usage(self):
        instance = self._fake_instance_obj(host='fakehost', memory_mb=5,
                root_gb=10)
        self.tracker.update_usage(self.context, instance)

    def test_disabled_resize_claim(self):
        instance = self._fake_instance_obj()
        instance_type = self._fake_flavor_create()
        claim = self.tracker.resize_claim(self.context, instance,
                instance_type)
        self.assertEqual(0, claim.memory_mb)
        self.assertEqual(instance['uuid'], claim.migration['instance_uuid'])
        self.assertEqual(instance_type['id'],
                claim.migration['new_instance_type_id'])

    def test_disabled_resize_context_claim(self):
        instance = self._fake_instance_obj()
        instance_type = self._fake_flavor_create()
        with self.tracker.resize_claim(self.context, instance, instance_type) \
                                       as claim:
            self.assertEqual(0, claim.memory_mb)


class MissingServiceTestCase(BaseTestCase):
    def setUp(self):
        super(MissingServiceTestCase, self).setUp()
        self.context = context.get_admin_context()
        self.tracker = self._tracker()

    def test_missing_service(self):
        self.tracker.compute_node = None
        self.tracker._get_service = mock.Mock(return_value=None)
        self.tracker.update_available_resource(self.context)
        self.assertTrue(self.tracker.disabled)


class MissingComputeNodeTestCase(BaseTestCase):
    def setUp(self):
        super(MissingComputeNodeTestCase, self).setUp()
        self.tracker = self._tracker()

        self.stubs.Set(db, 'service_get_by_compute_host',
                self._fake_service_get_by_compute_host)
        self.stubs.Set(db, 'compute_node_get_by_host_and_nodename',
                self._fake_compute_node_get_by_host_and_nodename)
        self.stubs.Set(db, 'compute_node_create',
                self._fake_create_compute_node)
        self.tracker.scheduler_client.update_resource_stats = mock.Mock()

    def _fake_create_compute_node(self, context, values):
        self.created = True
        return self._create_compute_node(values)

    def _fake_service_get_by_compute_host(self, ctx, host):
        # return a service with no joined compute
        service = self._create_service()
        return service

    def _fake_compute_node_get_by_host_and_nodename(self, ctx, host, nodename):
        # return no compute node
        raise exception.ComputeHostNotFound(host=host)

    def test_create_compute_node(self):
        self.tracker.compute_node = None
        self.tracker.update_available_resource(self.context)
        self.assertTrue(self.created)

    def test_enabled(self):
        self.tracker.update_available_resource(self.context)
        self.assertFalse(self.tracker.disabled)


class BaseTrackerTestCase(BaseTestCase):

    def setUp(self):
        # setup plumbing for a working resource tracker with required
        # database models and a compatible compute driver:
        super(BaseTrackerTestCase, self).setUp()

        self.tracker = self._tracker()
        self._migrations = {}

        self.stubs.Set(db, 'service_get_by_compute_host',
                self._fake_service_get_by_compute_host)
        self.stubs.Set(db, 'compute_node_get_by_host_and_nodename',
                self._fake_compute_node_get_by_host_and_nodename)
        self.stubs.Set(db, 'compute_node_update',
                self._fake_compute_node_update)
        self.stubs.Set(db, 'compute_node_delete',
                self._fake_compute_node_delete)
        self.stubs.Set(db, 'migration_update',
                self._fake_migration_update)
        self.stubs.Set(db, 'migration_get_in_progress_by_host_and_node',
                self._fake_migration_get_in_progress_by_host_and_node)

        # Note that this must be called before the call to _init_tracker()
        patcher = pci_fakes.fake_pci_whitelist()
        self.addCleanup(patcher.stop)

        self._init_tracker()
        self.limits = self._limits()

    def _fake_service_get_by_compute_host(self, ctx, host):
        self.service = self._create_service(host, compute=self.compute)
        return self.service

    def _fake_compute_node_get_by_host_and_nodename(self, ctx, host, nodename):
        self.compute = self._create_compute_node()
        return self.compute

    def _fake_compute_node_update(self, ctx, compute_node_id, values,
            prune_stats=False):
        self.update_call_count += 1
        self.updated = True
        self.compute.update(values)
        return self.compute

    def _fake_compute_node_delete(self, ctx, compute_node_id):
        self.deleted = True
        self.compute.update({'deleted': 1})
        return self.compute

    def _fake_migration_get_in_progress_by_host_and_node(self, ctxt, host,
                                                         node):
        status = ['confirmed', 'reverted', 'error']
        migrations = []

        for migration in self._migrations.values():
            migration = obj_base.obj_to_primitive(migration)
            if migration['status'] in status:
                continue

            uuid = migration['instance_uuid']
            migration['instance'] = self._instances[uuid]
            migrations.append(migration)

        return migrations

    def _fake_migration_update(self, ctxt, migration_id, values):
        # cheat and assume there's only 1 migration present
        migration = self._migrations.values()[0]
        migration.update(values)
        return migration

    def _init_tracker(self):
        self.tracker.update_available_resource(self.context)

    def _limits(self, memory_mb=FAKE_VIRT_MEMORY_WITH_OVERHEAD,
            disk_gb=FAKE_VIRT_LOCAL_GB,
            vcpus=FAKE_VIRT_VCPUS,
            numa_topology=FAKE_VIRT_NUMA_TOPOLOGY_OVERHEAD):
        """Create limits dictionary used for oversubscribing resources."""

        return {
            'memory_mb': memory_mb,
            'disk_gb': disk_gb,
            'vcpu': vcpus,
            'numa_topology': numa_topology,
        }

    def assertEqualNUMAHostTopology(self, expected, got):
        attrs = ('cpuset', 'memory', 'id', 'cpu_usage', 'memory_usage')
        if None in (expected, got):
            if expected != got:
                raise AssertionError("Topologies don't match. Expected: "
                                     "%(expected)s, but got: %(got)s" %
                                     {'expected': expected, 'got': got})
            else:
                return

        if len(expected) != len(got):
            raise AssertionError("Topologies don't match due to different "
                                 "number of cells. Expected: "
                                 "%(expected)s, but got: %(got)s" %
                                 {'expected': expected, 'got': got})
        for exp_cell, got_cell in zip(expected.cells, got.cells):
            for attr in attrs:
                if getattr(exp_cell, attr) != getattr(got_cell, attr):
                    raise AssertionError("Topologies don't match. Expected: "
                                         "%(expected)s, but got: %(got)s" %
                                         {'expected': expected, 'got': got})

    def assertEqualPciDevicePool(self, expected, observed):
        self.assertEqual(expected.product_id, observed.product_id)
        self.assertEqual(expected.vendor_id, observed.vendor_id)
        self.assertEqual(expected.tags, observed.tags)
        self.assertEqual(expected.count, observed.count)

    def assertEqualPciDevicePoolList(self, expected, observed):
        ex_objs = expected.objects
        ob_objs = observed.objects
        self.assertEqual(len(ex_objs), len(ob_objs))
        for i in range(len(ex_objs)):
            self.assertEqualPciDevicePool(ex_objs[i], ob_objs[i])

    def _assert(self, value, field, tracker=None):

        if tracker is None:
            tracker = self.tracker

        if field not in tracker.compute_node:
            raise test.TestingException(
                "'%(field)s' not in compute node." % {'field': field})
        x = tracker.compute_node[field]

        if field == 'numa_topology':
            self.assertEqualNUMAHostTopology(
                    value, objects.NUMATopology.obj_from_db_obj(x))
        else:
            self.assertEqual(value, x)


class TrackerTestCase(BaseTrackerTestCase):

    def test_free_ram_resource_value(self):
        driver = FakeVirtDriver()
        mem_free = driver.memory_mb - driver.memory_mb_used
        self.assertEqual(mem_free, self.tracker.compute_node.free_ram_mb)

    def test_free_disk_resource_value(self):
        driver = FakeVirtDriver()
        mem_free = driver.local_gb - driver.local_gb_used
        self.assertEqual(mem_free, self.tracker.compute_node.free_disk_gb)

    def test_update_compute_node(self):
        self.assertFalse(self.tracker.disabled)
        self.assertTrue(self.updated)

    def test_init(self):
        driver = self._driver()
        self._assert(FAKE_VIRT_MEMORY_MB, 'memory_mb')
        self._assert(FAKE_VIRT_LOCAL_GB, 'local_gb')
        self._assert(FAKE_VIRT_VCPUS, 'vcpus')
        self._assert(FAKE_VIRT_NUMA_TOPOLOGY, 'numa_topology')
        self._assert(0, 'memory_mb_used')
        self._assert(0, 'local_gb_used')
        self._assert(0, 'vcpus_used')
        self._assert(0, 'running_vms')
        self._assert(FAKE_VIRT_MEMORY_MB, 'free_ram_mb')
        self._assert(FAKE_VIRT_LOCAL_GB, 'free_disk_gb')
        self.assertFalse(self.tracker.disabled)
        self.assertEqual(0, self.tracker.compute_node.current_workload)
        expected = pci_device_pool.from_pci_stats(driver.pci_stats)
        self.assertEqual(expected,
                         self.tracker.compute_node.pci_device_pools)

    def test_set_instance_host_and_node(self):
        inst = objects.Instance()
        with mock.patch.object(inst, 'save') as mock_save:
            self.tracker._set_instance_host_and_node(self.context, inst)
            mock_save.assert_called_once_with()
        self.assertEqual(self.tracker.host, inst.host)
        self.assertEqual(self.tracker.nodename, inst.node)
        self.assertEqual(self.tracker.host, inst.launched_on)


class SchedulerClientTrackerTestCase(BaseTrackerTestCase):

    def setUp(self):
        super(SchedulerClientTrackerTestCase, self).setUp()
        self.tracker.scheduler_client.update_resource_stats = mock.Mock()

    def test_update_resource(self):
        # NOTE(pmurray): we are not doing a full pass through the resource
        # trackers update path, so safest to do two updates and look for
        # differences then to rely on the initial state being the same
        # as an update
        urs_mock = self.tracker.scheduler_client.update_resource_stats
        self.tracker._update(self.context)
        urs_mock.reset_mock()
        # change a compute node value to simulate a change
        self.tracker.compute_node.local_gb_used += 1
        self.tracker._update(self.context)
        urs_mock.assert_called_once_with(self.tracker.compute_node)

    def test_no_update_resource(self):
        # NOTE(pmurray): we are not doing a full pass through the resource
        # trackers update path, so safest to do two updates and look for
        # differences then to rely on the initial state being the same
        # as an update
        self.tracker._update(self.context)
        update = self.tracker.scheduler_client.update_resource_stats
        update.reset_mock()
        self.tracker._update(self.context)
        self.assertFalse(update.called, "update_resource_stats should not be "
                                        "called when there is no change")


class TrackerPciStatsTestCase(BaseTrackerTestCase):

    def test_update_compute_node(self):
        self.assertFalse(self.tracker.disabled)
        self.assertTrue(self.updated)

    def test_init(self):
        driver = self._driver()
        self._assert(FAKE_VIRT_MEMORY_MB, 'memory_mb')
        self._assert(FAKE_VIRT_LOCAL_GB, 'local_gb')
        self._assert(FAKE_VIRT_VCPUS, 'vcpus')
        self._assert(FAKE_VIRT_NUMA_TOPOLOGY, 'numa_topology')
        self._assert(0, 'memory_mb_used')
        self._assert(0, 'local_gb_used')
        self._assert(0, 'vcpus_used')
        self._assert(0, 'running_vms')
        self._assert(FAKE_VIRT_MEMORY_MB, 'free_ram_mb')
        self._assert(FAKE_VIRT_LOCAL_GB, 'free_disk_gb')
        self.assertFalse(self.tracker.disabled)
        self.assertEqual(0, self.tracker.compute_node.current_workload)
        expected_pools = pci_device_pool.from_pci_stats(driver.pci_stats)
        observed_pools = self.tracker.compute_node.pci_device_pools
        self.assertEqualPciDevicePoolList(expected_pools, observed_pools)

    def _driver(self):
        return FakeVirtDriver(pci_support=True)


class TrackerExtraResourcesTestCase(BaseTrackerTestCase):

    def test_set_empty_ext_resources(self):
        resources = self._create_compute_node_obj(self.context)
        del resources.stats
        self.tracker._write_ext_resources(resources)
        self.assertEqual({}, resources.stats)

    def test_set_extra_resources(self):
        def fake_write_resources(resources):
            resources['stats']['resA'] = '123'
            resources['stats']['resB'] = 12

        self.stubs.Set(self.tracker.ext_resources_handler,
                       'write_resources',
                       fake_write_resources)

        resources = self._create_compute_node_obj(self.context)
        del resources.stats
        self.tracker._write_ext_resources(resources)

        expected = {"resA": "123", "resB": "12"}
        self.assertEqual(sorted(expected),
                         sorted(resources.stats))


class InstanceClaimTestCase(BaseTrackerTestCase):
    def _instance_topology(self, mem):
        mem = mem * 1024
        return objects.InstanceNUMATopology(
            cells=[objects.InstanceNUMACell(
                id=0, cpuset=set([1]), memory=mem),
                   objects.InstanceNUMACell(
                       id=1, cpuset=set([3]), memory=mem)])

    def _claim_topology(self, mem, cpus=1):
        if self.tracker.driver.numa_topology is None:
            return None
        mem = mem * 1024
        return objects.NUMATopology(
            cells=[objects.NUMACell(
                       id=0, cpuset=set([1, 2]), memory=3072, cpu_usage=cpus,
                       memory_usage=mem, mempages=[], siblings=[],
                       pinned_cpus=set([])),
                   objects.NUMACell(
                       id=1, cpuset=set([3, 4]), memory=3072, cpu_usage=cpus,
                       memory_usage=mem, mempages=[], siblings=[],
                       pinned_cpus=set([]))])

    @mock.patch('nova.objects.InstancePCIRequests.get_by_instance_uuid',
                return_value=objects.InstancePCIRequests(requests=[]))
    def test_update_usage_only_for_tracked(self, mock_get):
        flavor = self._fake_flavor_create()
        claim_mem = flavor['memory_mb'] + FAKE_VIRT_MEMORY_OVERHEAD
        claim_gb = flavor['root_gb'] + flavor['ephemeral_gb']
        claim_topology = self._claim_topology(claim_mem / 2)

        instance_topology = self._instance_topology(claim_mem / 2)

        instance = self._fake_instance_obj(
                flavor=flavor, task_state=None,
                numa_topology=instance_topology)
        self.tracker.update_usage(self.context, instance)

        self._assert(0, 'memory_mb_used')
        self._assert(0, 'local_gb_used')
        self._assert(0, 'current_workload')
        self._assert(FAKE_VIRT_NUMA_TOPOLOGY, 'numa_topology')

        with mock.patch.object(instance, 'save'):
            claim = self.tracker.instance_claim(self.context, instance,
                                                self.limits)
        self.assertNotEqual(0, claim.memory_mb)
        self._assert(claim_mem, 'memory_mb_used')
        self._assert(claim_gb, 'local_gb_used')
        self._assert(claim_topology, 'numa_topology')

        # now update should actually take effect
        instance['task_state'] = task_states.SCHEDULING
        self.tracker.update_usage(self.context, instance)

        self._assert(claim_mem, 'memory_mb_used')
        self._assert(claim_gb, 'local_gb_used')
        self._assert(claim_topology, 'numa_topology')
        self._assert(1, 'current_workload')

    @mock.patch('nova.objects.InstancePCIRequests.get_by_instance_uuid',
                return_value=objects.InstancePCIRequests(requests=[]))
    def test_claim_and_abort(self, mock_get):
        claim_mem = 3
        claim_mem_total = 3 + FAKE_VIRT_MEMORY_OVERHEAD
        claim_disk = 2
        claim_topology = self._claim_topology(claim_mem_total / 2)

        instance_topology = self._instance_topology(claim_mem_total / 2)
        instance = self._fake_instance_obj(memory_mb=claim_mem,
                root_gb=claim_disk, ephemeral_gb=0,
                numa_topology=instance_topology)

        with mock.patch.object(instance, 'save'):
            claim = self.tracker.instance_claim(self.context, instance,
                                                self.limits)
        self.assertIsNotNone(claim)

        self.assertEqual(claim_mem_total, self.compute["memory_mb_used"])
        self.assertEqual(FAKE_VIRT_MEMORY_MB - claim_mem_total,
                         self.compute["free_ram_mb"])
        self.assertEqualNUMAHostTopology(
                claim_topology, objects.NUMATopology.obj_from_db_obj(
                    self.compute['numa_topology']))

        self.assertEqual(claim_disk, self.compute["local_gb_used"])
        self.assertEqual(FAKE_VIRT_LOCAL_GB - claim_disk,
                         self.compute["free_disk_gb"])

        claim.abort()

        self.assertEqual(0, self.compute["memory_mb_used"])
        self.assertEqual(FAKE_VIRT_MEMORY_MB, self.compute["free_ram_mb"])
        self.assertEqualNUMAHostTopology(
                FAKE_VIRT_NUMA_TOPOLOGY,
                objects.NUMATopology.obj_from_db_obj(
                    self.compute['numa_topology']))

        self.assertEqual(0, self.compute["local_gb_used"])
        self.assertEqual(FAKE_VIRT_LOCAL_GB, self.compute["free_disk_gb"])

    @mock.patch('nova.objects.InstancePCIRequests.get_by_instance_uuid',
                return_value=objects.InstancePCIRequests(requests=[]))
    def test_instance_claim_with_oversubscription(self, mock_get):
        memory_mb = FAKE_VIRT_MEMORY_MB * 2
        root_gb = ephemeral_gb = FAKE_VIRT_LOCAL_GB
        vcpus = FAKE_VIRT_VCPUS * 2
        claim_topology = self._claim_topology(3)
        instance_topology = self._instance_topology(3)

        limits = {'memory_mb': memory_mb + FAKE_VIRT_MEMORY_OVERHEAD,
                  'disk_gb': root_gb * 2,
                  'vcpu': vcpus,
                  'numa_topology': FAKE_VIRT_NUMA_TOPOLOGY_OVERHEAD}

        instance = self._fake_instance_obj(memory_mb=memory_mb,
                root_gb=root_gb, ephemeral_gb=ephemeral_gb,
                numa_topology=instance_topology)

        with mock.patch.object(instance, 'save'):
            self.tracker.instance_claim(self.context, instance, limits)
        self.assertEqual(memory_mb + FAKE_VIRT_MEMORY_OVERHEAD,
                self.tracker.compute_node.memory_mb_used)
        self.assertEqualNUMAHostTopology(
                claim_topology,
                objects.NUMATopology.obj_from_db_obj(
                    self.compute['numa_topology']))
        self.assertEqual(root_gb * 2,
                self.tracker.compute_node.local_gb_used)

    @mock.patch('nova.objects.InstancePCIRequests.get_by_instance_uuid',
                return_value=objects.InstancePCIRequests(requests=[]))
    @mock.patch('nova.objects.Instance.save')
    def test_additive_claims(self, mock_save, mock_get):
        self.limits['vcpu'] = 2
        claim_topology = self._claim_topology(2, cpus=2)

        flavor = self._fake_flavor_create(
                memory_mb=1, root_gb=1, ephemeral_gb=0)
        instance_topology = self._instance_topology(1)
        instance = self._fake_instance_obj(
                flavor=flavor, numa_topology=instance_topology)
        with self.tracker.instance_claim(self.context, instance, self.limits):
            pass
        instance = self._fake_instance_obj(
                flavor=flavor, numa_topology=instance_topology)
        with self.tracker.instance_claim(self.context, instance, self.limits):
            pass

        self.assertEqual(2 * (flavor['memory_mb'] + FAKE_VIRT_MEMORY_OVERHEAD),
                self.tracker.compute_node.memory_mb_used)
        self.assertEqual(2 * (flavor['root_gb'] + flavor['ephemeral_gb']),
                self.tracker.compute_node.local_gb_used)
        self.assertEqual(2 * flavor['vcpus'],
                self.tracker.compute_node.vcpus_used)

        self.assertEqualNUMAHostTopology(
                claim_topology,
                objects.NUMATopology.obj_from_db_obj(
                    self.compute['numa_topology']))

    @mock.patch('nova.objects.InstancePCIRequests.get_by_instance_uuid',
                return_value=objects.InstancePCIRequests(requests=[]))
    @mock.patch('nova.objects.Instance.save')
    def test_context_claim_with_exception(self, mock_save, mock_get):
        instance = self._fake_instance_obj(memory_mb=1, root_gb=1,
                                           ephemeral_gb=1)
        try:
            with self.tracker.instance_claim(self.context, instance):
                # <insert exciting things that utilize resources>
                raise test.TestingException()
        except test.TestingException:
            pass

        self.assertEqual(0, self.tracker.compute_node.memory_mb_used)
        self.assertEqual(0, self.tracker.compute_node.local_gb_used)
        self.assertEqual(0, self.compute['memory_mb_used'])
        self.assertEqual(0, self.compute['local_gb_used'])
        self.assertEqualNUMAHostTopology(
                FAKE_VIRT_NUMA_TOPOLOGY,
                objects.NUMATopology.obj_from_db_obj(
                    self.compute['numa_topology']))

    @mock.patch('nova.objects.InstancePCIRequests.get_by_instance_uuid',
                return_value=objects.InstancePCIRequests(requests=[]))
    @mock.patch('nova.objects.Instance.save')
    @mock.patch('nova.objects.InstanceList.get_by_host_and_node')
    def test_instance_context_claim(self, mock_get_all, mock_save, mock_get):
        flavor = self._fake_flavor_create(
                memory_mb=1, root_gb=2, ephemeral_gb=3)
        claim_topology = self._claim_topology(1)

        instance_topology = self._instance_topology(1)
        instance = self._fake_instance_obj(
                flavor=flavor, numa_topology=instance_topology)
        with self.tracker.instance_claim(self.context, instance):
            # <insert exciting things that utilize resources>
            self.assertEqual(flavor['memory_mb'] + FAKE_VIRT_MEMORY_OVERHEAD,
                             self.tracker.compute_node.memory_mb_used)
            self.assertEqual(flavor['root_gb'] + flavor['ephemeral_gb'],
                             self.tracker.compute_node.local_gb_used)
            self.assertEqual(flavor['memory_mb'] + FAKE_VIRT_MEMORY_OVERHEAD,
                             self.compute['memory_mb_used'])
            self.assertEqualNUMAHostTopology(
                    claim_topology,
                    objects.NUMATopology.obj_from_db_obj(
                        self.compute['numa_topology']))
            self.assertEqual(flavor['root_gb'] + flavor['ephemeral_gb'],
                             self.compute['local_gb_used'])

        # after exiting claim context, build is marked as finished.  usage
        # totals should be same:
        mock_get_all.return_value = [instance]
        self.tracker.update_available_resource(self.context)
        self.assertEqual(flavor['memory_mb'] + FAKE_VIRT_MEMORY_OVERHEAD,
                         self.tracker.compute_node.memory_mb_used)
        self.assertEqual(flavor['root_gb'] + flavor['ephemeral_gb'],
                         self.tracker.compute_node.local_gb_used)
        self.assertEqual(flavor['memory_mb'] + FAKE_VIRT_MEMORY_OVERHEAD,
                         self.compute['memory_mb_used'])
        self.assertEqualNUMAHostTopology(
                claim_topology,
                objects.NUMATopology.obj_from_db_obj(
                    self.compute['numa_topology']))
        self.assertEqual(flavor['root_gb'] + flavor['ephemeral_gb'],
                         self.compute['local_gb_used'])

    @mock.patch('nova.objects.InstancePCIRequests.get_by_instance_uuid',
                return_value=objects.InstancePCIRequests(requests=[]))
    def test_update_load_stats_for_instance(self, mock_get):
        instance = self._fake_instance_obj(task_state=task_states.SCHEDULING)
        with mock.patch.object(instance, 'save'):
            with self.tracker.instance_claim(self.context, instance):
                pass

        self.assertEqual(1, self.tracker.compute_node.current_workload)

        instance['vm_state'] = vm_states.ACTIVE
        instance['task_state'] = None
        instance['host'] = 'fakehost'

        self.tracker.update_usage(self.context, instance)
        self.assertEqual(0, self.tracker.compute_node.current_workload)

    @mock.patch('nova.objects.InstancePCIRequests.get_by_instance_uuid',
                return_value=objects.InstancePCIRequests(requests=[]))
    @mock.patch('nova.objects.Instance.save')
    def test_cpu_stats(self, mock_save, mock_get):
        limits = {'disk_gb': 100, 'memory_mb': 100}
        self.assertEqual(0, self.tracker.compute_node.vcpus_used)

        vcpus = 1
        instance = self._fake_instance_obj(vcpus=vcpus)

        # should not do anything until a claim is made:
        self.tracker.update_usage(self.context, instance)
        self.assertEqual(0, self.tracker.compute_node.vcpus_used)

        with self.tracker.instance_claim(self.context, instance, limits):
            pass
        self.assertEqual(vcpus, self.tracker.compute_node.vcpus_used)

        # instance state can change without modifying vcpus in use:
        instance['task_state'] = task_states.SCHEDULING
        self.tracker.update_usage(self.context, instance)
        self.assertEqual(vcpus, self.tracker.compute_node.vcpus_used)

        add_vcpus = 10
        vcpus += add_vcpus
        instance = self._fake_instance_obj(vcpus=add_vcpus)
        with self.tracker.instance_claim(self.context, instance, limits):
            pass
        self.assertEqual(vcpus, self.tracker.compute_node.vcpus_used)

        instance['vm_state'] = vm_states.DELETED
        self.tracker.update_usage(self.context, instance)
        vcpus -= add_vcpus
        self.assertEqual(vcpus, self.tracker.compute_node.vcpus_used)

    def test_skip_deleted_instances(self):
        # ensure that the audit process skips instances that have vm_state
        # DELETED, but the DB record is not yet deleted.
        self._fake_instance_obj(vm_state=vm_states.DELETED, host=self.host)
        self.tracker.update_available_resource(self.context)

        self.assertEqual(0, self.tracker.compute_node.memory_mb_used)
        self.assertEqual(0, self.tracker.compute_node.local_gb_used)

    @mock.patch('nova.objects.MigrationList.get_in_progress_by_host_and_node')
    def test_deleted_instances_with_migrations(self, mock_migration_list):
        migration = objects.Migration(context=self.context,
                                      migration_type='resize',
                                      instance_uuid='invalid')
        mock_migration_list.return_value = [migration]
        self.tracker.update_available_resource(self.context)
        self.assertEqual(0, self.tracker.compute_node.memory_mb_used)
        self.assertEqual(0, self.tracker.compute_node.local_gb_used)
        mock_migration_list.assert_called_once_with(self.context,
                                                    "fakehost",
                                                    "fakenode")

    @mock.patch('nova.objects.MigrationList.get_in_progress_by_host_and_node')
    def test_instances_with_live_migrations(self, mock_migration_list):
        instance = self._fake_instance_obj()
        migration = objects.Migration(context=self.context,
                                      migration_type='live-migration',
                                      instance_uuid=instance.uuid)
        mock_migration_list.return_value = [migration]
        self.tracker.update_available_resource(self.context)
        self.assertEqual(0, self.tracker.compute_node['memory_mb_used'])
        self.assertEqual(0, self.tracker.compute_node['local_gb_used'])
        mock_migration_list.assert_called_once_with(self.context,
                                                    "fakehost",
                                                    "fakenode")

    @mock.patch('nova.compute.claims.Claim')
    @mock.patch('nova.objects.Instance.save')
    def test_claim_saves_numa_topology(self, mock_save, mock_claim):
        def fake_save():
            self.assertEqual(set(['numa_topology', 'host', 'node',
                                  'launched_on']),
                             inst.obj_what_changed())

        mock_save.side_effect = fake_save
        inst = objects.Instance(host=None, node=None, memory_mb=1024)
        inst.obj_reset_changes()
        numa = objects.InstanceNUMATopology()
        claim = mock.MagicMock()
        claim.claimed_numa_topology = numa
        mock_claim.return_value = claim
        with mock.patch.object(self.tracker, '_update_usage_from_instance'):
            self.tracker.instance_claim(self.context, inst)
        mock_save.assert_called_once_with()

    @mock.patch('nova.objects.InstancePCIRequests.get_by_instance_uuid',
                return_value=objects.InstancePCIRequests(requests=[]))
    def test_claim_sets_instance_host_and_node(self, mock_get):
        instance = self._fake_instance_obj()
        self.assertIsNone(instance['host'])
        self.assertIsNone(instance['launched_on'])
        self.assertIsNone(instance['node'])

        with mock.patch.object(instance, 'save'):
            claim = self.tracker.instance_claim(self.context, instance)
        self.assertNotEqual(0, claim.memory_mb)

        self.assertEqual('fakehost', instance['host'])
        self.assertEqual('fakehost', instance['launched_on'])
        self.assertEqual('fakenode', instance['node'])


class MoveClaimTestCase(BaseTrackerTestCase):

    def setUp(self):
        super(MoveClaimTestCase, self).setUp()

        self.instance = self._fake_instance_obj()
        self.instance_type = self._fake_flavor_create()

    @mock.patch('nova.objects.InstancePCIRequests.get_by_instance_uuid',
                return_value=objects.InstancePCIRequests(requests=[]))
    def test_claim(self, mock_get):
        self.tracker.resize_claim(self.context, self.instance,
                self.instance_type, self.limits)
        self._assert(FAKE_VIRT_MEMORY_WITH_OVERHEAD, 'memory_mb_used')
        self._assert(FAKE_VIRT_LOCAL_GB, 'local_gb_used')
        self._assert(FAKE_VIRT_VCPUS, 'vcpus_used')
        self.assertEqual(1, len(self.tracker.tracked_migrations))

    @mock.patch('nova.objects.InstancePCIRequests.get_by_instance_uuid',
                return_value=objects.InstancePCIRequests(requests=[]))
    def test_abort(self, mock_get):
        try:
            with self.tracker.resize_claim(self.context, self.instance,
                    self.instance_type, self.limits):
                raise test.TestingException("abort")
        except test.TestingException:
            pass

        self._assert(0, 'memory_mb_used')
        self._assert(0, 'local_gb_used')
        self._assert(0, 'vcpus_used')
        self.assertEqual(0, len(self.tracker.tracked_migrations))

    @mock.patch('nova.objects.InstancePCIRequests.get_by_instance_uuid',
                return_value=objects.InstancePCIRequests(requests=[]))
    def test_additive_claims(self, mock_get):

        limits = self._limits(
              2 * FAKE_VIRT_MEMORY_WITH_OVERHEAD,
              2 * FAKE_VIRT_LOCAL_GB,
              2 * FAKE_VIRT_VCPUS)
        self.tracker.resize_claim(self.context, self.instance,
                self.instance_type, limits)
        instance2 = self._fake_instance_obj()
        self.tracker.resize_claim(self.context, instance2, self.instance_type,
                limits)

        self._assert(2 * FAKE_VIRT_MEMORY_WITH_OVERHEAD, 'memory_mb_used')
        self._assert(2 * FAKE_VIRT_LOCAL_GB, 'local_gb_used')
        self._assert(2 * FAKE_VIRT_VCPUS, 'vcpus_used')

    @mock.patch('nova.objects.InstancePCIRequests.get_by_instance_uuid',
                return_value=objects.InstancePCIRequests(requests=[]))
    def test_revert(self, mock_get):
        self.tracker.resize_claim(self.context, self.instance,
                self.instance_type, {}, self.limits)
        self.tracker.drop_move_claim(self.context, self.instance)

        self.assertEqual(0, len(self.tracker.tracked_instances))
        self.assertEqual(0, len(self.tracker.tracked_migrations))
        self._assert(0, 'memory_mb_used')
        self._assert(0, 'local_gb_used')
        self._assert(0, 'vcpus_used')

    def test_resize_filter(self):
        instance = self._fake_instance_obj(vm_state=vm_states.ACTIVE,
                task_state=task_states.SUSPENDING)
        self.assertFalse(self.tracker._instance_in_resize_state(instance))

        instance = self._fake_instance_obj(vm_state=vm_states.RESIZED,
                task_state=task_states.SUSPENDING)
        self.assertTrue(self.tracker._instance_in_resize_state(instance))

        states = [task_states.RESIZE_PREP, task_states.RESIZE_MIGRATING,
                  task_states.RESIZE_MIGRATED, task_states.RESIZE_FINISH]
        for vm_state in [vm_states.ACTIVE, vm_states.STOPPED]:
            for task_state in states:
                instance = self._fake_instance_obj(vm_state=vm_state,
                                                   task_state=task_state)
                result = self.tracker._instance_in_resize_state(instance)
                self.assertTrue(result)


class OrphanTestCase(BaseTrackerTestCase):
    def _driver(self):
        class OrphanVirtDriver(FakeVirtDriver):
            def get_per_instance_usage(self):
                return {
                    '1-2-3-4-5': {'memory_mb': FAKE_VIRT_MEMORY_MB,
                                  'uuid': '1-2-3-4-5'},
                    '2-3-4-5-6': {'memory_mb': FAKE_VIRT_MEMORY_MB,
                                  'uuid': '2-3-4-5-6'},
                }

        return OrphanVirtDriver()

    def test_usage(self):
        self.assertEqual(2 * FAKE_VIRT_MEMORY_WITH_OVERHEAD,
                self.tracker.compute_node.memory_mb_used)

    def test_find(self):
        # create one legit instance and verify the 2 orphans remain
        self._fake_instance_obj()
        orphans = self.tracker._find_orphaned_instances()

        self.assertEqual(2, len(orphans))


class ComputeMonitorTestCase(BaseTestCase):
    def setUp(self):
        super(ComputeMonitorTestCase, self).setUp()
        self.tracker = self._tracker()
        self.node_name = 'nodename'
        self.user_id = 'fake'
        self.project_id = 'fake'
        self.info = {}
        self.context = context.RequestContext(self.user_id,
                                              self.project_id)

    def test_get_host_metrics_none(self):
        self.tracker.monitors = []
        metrics = self.tracker._get_host_metrics(self.context,
                                                 self.node_name)
        self.assertEqual(len(metrics), 0)

    @mock.patch.object(resource_tracker.LOG, 'warning')
    def test_get_host_metrics_exception(self, mock_LOG_warning):
        monitor = mock.MagicMock()
        monitor.add_metrics_to_list.side_effect = Exception
        self.tracker.monitors = [monitor]
        metrics = self.tracker._get_host_metrics(self.context,
                                                 self.node_name)
        mock_LOG_warning.assert_called_once_with(
            u'Cannot get the metrics from %s.', mock.ANY)
        self.assertEqual(0, len(metrics))

    def test_get_host_metrics(self):
        class FakeCPUMonitor(monitor_base.MonitorBase):

            NOW_TS = timeutils.utcnow()

            def __init__(self, *args):
                super(FakeCPUMonitor, self).__init__(*args)
                self.source = 'FakeCPUMonitor'

            def get_metric_names(self):
                return set(["cpu.frequency"])

            def get_metric(self, name):
                return 100, self.NOW_TS

        self.tracker.monitors = [FakeCPUMonitor(None)]
        mock_notifier = mock.Mock()

        with mock.patch.object(rpc, 'get_notifier',
                               return_value=mock_notifier) as mock_get:
            metrics = self.tracker._get_host_metrics(self.context,
                                                     self.node_name)
            mock_get.assert_called_once_with(service='compute',
                                             host=self.node_name)

        expected_metrics = [
            {
                'timestamp': timeutils.strtime(
                    FakeCPUMonitor.NOW_TS),
                'name': 'cpu.frequency',
                'value': 100,
                'source': 'FakeCPUMonitor'
            },
        ]

        payload = {
            'metrics': expected_metrics,
            'host': self.tracker.host,
            'host_ip': CONF.my_ip,
            'nodename': self.node_name
        }

        mock_notifier.info.assert_called_once_with(
            self.context, 'compute.metrics.update', payload)

        self.assertEqual(metrics, expected_metrics)


class TrackerPeriodicTestCase(BaseTrackerTestCase):

    def test_periodic_status_update(self):
        # verify update called on instantiation
        self.assertEqual(1, self.update_call_count)

        # verify update not called if no change to resources
        self.tracker.update_available_resource(self.context)
        self.assertEqual(1, self.update_call_count)

        # verify update is called when resources change
        driver = self.tracker.driver
        driver.memory_mb += 1
        self.tracker.update_available_resource(self.context)
        self.assertEqual(2, self.update_call_count)

    def test_update_available_resource_calls_locked_inner(self):
        @mock.patch.object(self.tracker, 'driver')
        @mock.patch.object(self.tracker,
                           '_update_available_resource')
        @mock.patch.object(self.tracker, '_verify_resources')
        @mock.patch.object(self.tracker, '_report_hypervisor_resource_view')
        def _test(mock_rhrv, mock_vr, mock_uar, mock_driver):
            resources = {'there is someone in my head': 'but it\'s not me'}
            mock_driver.get_available_resource.return_value = resources
            self.tracker.update_available_resource(self.context)
            mock_uar.assert_called_once_with(self.context, resources)

        _test()


class StatsDictTestCase(BaseTrackerTestCase):
    """Test stats handling for a virt driver that provides
    stats as a dictionary.
    """
    def _driver(self):
        return FakeVirtDriver(stats=FAKE_VIRT_STATS)

    def test_virt_stats(self):
        # start with virt driver stats
        stats = self.tracker.compute_node.stats
        self.assertEqual(FAKE_VIRT_STATS_COERCED, stats)

        # adding an instance should keep virt driver stats
        self._fake_instance_obj(vm_state=vm_states.ACTIVE, host=self.host)
        self.tracker.update_available_resource(self.context)

        stats = self.tracker.compute_node.stats
        # compute node stats are coerced to strings
        expected_stats = copy.deepcopy(FAKE_VIRT_STATS_COERCED)
        for k, v in self.tracker.stats.iteritems():
            expected_stats[k] = six.text_type(v)
        self.assertEqual(expected_stats, stats)

        # removing the instances should keep only virt driver stats
        self._instances = {}
        self.tracker.update_available_resource(self.context)

        stats = self.tracker.compute_node.stats
        self.assertEqual(FAKE_VIRT_STATS_COERCED, stats)


class StatsJsonTestCase(BaseTrackerTestCase):
    """Test stats handling for a virt driver that provides
    stats as a json string.
    """
    def _driver(self):
        return FakeVirtDriver(stats=FAKE_VIRT_STATS_JSON)

    def test_virt_stats(self):
        # start with virt driver stats
        stats = self.tracker.compute_node.stats
        self.assertEqual(FAKE_VIRT_STATS_COERCED, stats)

        # adding an instance should keep virt driver stats
        # and add rt stats
        self._fake_instance_obj(vm_state=vm_states.ACTIVE, host=self.host)
        self.tracker.update_available_resource(self.context)

        stats = self.tracker.compute_node.stats
        # compute node stats are coerced to strings
        expected_stats = copy.deepcopy(FAKE_VIRT_STATS_COERCED)
        for k, v in self.tracker.stats.iteritems():
            expected_stats[k] = six.text_type(v)
        self.assertEqual(expected_stats, stats)

        # removing the instances should keep only virt driver stats
        self._instances = {}
        self.tracker.update_available_resource(self.context)
        stats = self.tracker.compute_node.stats
        self.assertEqual(FAKE_VIRT_STATS_COERCED, stats)


class StatsInvalidJsonTestCase(BaseTrackerTestCase):
    """Test stats handling for a virt driver that provides
    an invalid type for stats.
    """
    def _driver(self):
        return FakeVirtDriver(stats='this is not json')

    def _init_tracker(self):
        # do not do initial update in setup
        pass

    def test_virt_stats(self):
        # should throw exception for string that does not parse as json
        self.assertRaises(ValueError,
                          self.tracker.update_available_resource,
                          context=self.context)


class StatsInvalidTypeTestCase(BaseTrackerTestCase):
    """Test stats handling for a virt driver that provides
    an invalid type for stats.
    """
    def _driver(self):
        return FakeVirtDriver(stats=10)

    def _init_tracker(self):
        # do not do initial update in setup
        pass

    def test_virt_stats(self):
        # should throw exception for incorrect stats value type
        self.assertRaises(ValueError,
                          self.tracker.update_available_resource,
                          context=self.context)
