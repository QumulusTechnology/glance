# Copyright 2018 RedHat, Inc.
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

import fixtures
import json
import os
from unittest import mock

import glance_store
from oslo_concurrency import processutils
from oslo_config import cfg

import glance.async_.flows.api_image_import as import_flow
import glance.async_.flows.plugins.image_conversion as image_conversion
from glance.async_ import utils as async_utils
from glance.common import format_inspector
from glance.common import utils
from glance import domain
from glance import gateway
import glance.tests.utils as test_utils

CONF = cfg.CONF


UUID1 = 'c80a1a6c-bd1f-41c5-90ee-81afedb1d58d'
TENANT1 = '6838eb7b-6ded-434a-882c-b344c77fe8df'


class TestConvertImageTask(test_utils.BaseTestCase):

    def setUp(self):
        super(TestConvertImageTask, self).setUp()

        glance_store.register_opts(CONF)
        self.config(default_store='file',
                    stores=['file', 'http'],
                    filesystem_store_datadir=self.test_dir,
                    group="glance_store")
        self.config(output_format='qcow2',
                    group='image_conversion')
        glance_store.create_stores(CONF)

        self.work_dir = os.path.join(self.test_dir, 'work_dir')
        utils.safe_mkdirs(self.work_dir)
        self.config(work_dir=self.work_dir, group='task')

        self.context = mock.MagicMock()
        self.img_repo = mock.MagicMock()
        self.task_repo = mock.MagicMock()
        self.stores = mock.MagicMock()
        self.image_id = UUID1

        self.gateway = gateway.Gateway()
        self.task_factory = domain.TaskFactory()
        self.img_factory = self.gateway.get_image_factory(self.context)
        self.image = self.img_factory.new_image(image_id=self.image_id,
                                                disk_format='raw',
                                                container_format='bare')

        task_input = {
            "import_from": "http://cloud.foo/image.raw",
            "import_from_format": "raw",
            "image_properties": {'disk_format': 'raw',
                                 'container_format': 'bare'}
        }

        task_ttl = CONF.task.task_time_to_live

        self.task_type = 'import'
        request_id = 'fake_request_id'
        user_id = 'fake_user'
        self.task = self.task_factory.new_task(self.task_type, TENANT1,
                                               self.image_id, user_id,
                                               request_id,
                                               task_time_to_live=task_ttl,
                                               task_input=task_input)

        self.image.extra_properties = {
            'os_glance_import_task': self.task.task_id,
            'os_glance_importing_to_stores': mock.MagicMock(),
            'os_glance_failed_import': ""
        }
        self.wrapper = import_flow.ImportActionWrapper(self.img_repo,
                                                       self.image_id,
                                                       self.task.task_id)

        self.detect_file_format_mock = mock.MagicMock()
        self.useFixture(fixtures.MockPatch('glance.common.format_inspector.'
                                           'detect_file_format',
                                           self.detect_file_format_mock))

    @mock.patch.object(os, 'stat')
    @mock.patch.object(os, 'remove')
    def test_image_convert_success(self, mock_os_remove, mock_os_stat):
        mock_os_remove.return_value = None
        mock_os_stat.return_value.st_size = 123
        image_convert = image_conversion._ConvertImage(self.context,
                                                       self.task.task_id,
                                                       self.task_type,
                                                       self.wrapper,
                                                       self.stores)

        self.task_repo.get.return_value = self.task
        image = mock.MagicMock(image_id=self.image_id, virtual_size=None,
                               extra_properties={
                                   'os_glance_import_task': self.task.task_id},
                               disk_format='raw')
        self.img_repo.get.return_value = image

        with mock.patch.object(processutils, 'execute') as exc_mock:
            exc_mock.return_value = ("", None)
            with mock.patch.object(json, 'loads') as jloads_mock:
                jloads_mock.return_value = {'format': 'raw',
                                            'virtual-size': 456}
                inspector = self.detect_file_format_mock.return_value
                inspector.__str__.return_value = 'raw'
                inspector.safety_check.return_value = True
                image_convert.execute('file:///test/path.raw')

                # NOTE(hemanthm): Asserting that the source format is passed
                # to qemu-utis to avoid inferring the image format. This
                # shields us from an attack vector described at
                # https://bugs.launchpad.net/glance/+bug/1449062/comments/72
                self.assertIn('-f', exc_mock.call_args[0])
                self.assertEqual("qcow2", image.disk_format)

        self.assertEqual('bare', image.container_format)
        self.assertEqual('qcow2', image.disk_format)
        self.assertEqual(456, image.virtual_size)
        self.assertEqual(123, image.size)

    @mock.patch('os.remove')
    @mock.patch('os.stat')
    @mock.patch('oslo_concurrency.processutils.trycmd')
    @mock.patch('glance.async_.flows.plugins.image_conversion.LOG')
    def test_image_convert_excluded_for_iso(self, mock_log, mock_run,
                                            mock_stat, mock_remove, fmt='iso'):
        mock_stat.return_value = mock.MagicMock(stsize=123)
        stdout = json.dumps({'format': 'raw'})
        mock_run.return_value = (stdout, '')
        image_convert = image_conversion._ConvertImage(self.context,
                                                       self.task.task_id,
                                                       self.task_type,
                                                       self.wrapper,
                                                       self.stores)
        self.task_repo.get.return_value = self.task
        image = mock.MagicMock(image_id=self.image_id, virtual_size=None,
                               extra_properties={
                                   'os_glance_import_task': self.task.task_id},
                               disk_format=fmt)

        self.img_repo.get.return_value = image
        inspector = self.detect_file_format_mock.return_value
        inspector.__str__.return_value = 'iso'
        inspector.safety_check.return_value = True

        image_convert.execute('file:///test/path.iso')
        self.assertEqual(fmt, image.disk_format)
        mock_log.debug.assert_called_once_with(
            "Avoiding conversion of an image %s having "
            "`iso` disk format.", self.image_id)

    def test_image_convert_iso_lie_raw(self):
        e = self.assertRaises(
            RuntimeError, self.test_image_convert_excluded_for_iso,
            fmt='raw')
        self.assertEqual('Image has disallowed configuration',
                         str(e))

    def test_image_convert_iso_lie_qcow2(self):
        e = self.assertRaises(
            RuntimeError, self.test_image_convert_excluded_for_iso,
            fmt='qcow2')
        self.assertEqual('Image has disallowed configuration',
                         str(e))

    def _setup_image_convert_info_fail(self, disk_format='qcow2'):
        image_convert = image_conversion._ConvertImage(self.context,
                                                       self.task.task_id,
                                                       self.task_type,
                                                       self.wrapper,
                                                       self.stores)

        self.task_repo.get.return_value = self.task
        image = mock.MagicMock(image_id=self.image_id, virtual_size=None,
                               extra_properties={
                                   'os_glance_import_task': self.task.task_id},
                               disk_format=disk_format)
        self.img_repo.get.return_value = image
        return image_convert

    def test_image_convert_fails_inspection(self):
        convert = self._setup_image_convert_info_fail()
        with mock.patch.object(processutils, 'execute') as exc_mock:
            inspector = self.detect_file_format_mock.return_value
            inspector.__str__.return_value = 'qcow2'
            inspector.safety_check.return_value = True
            exc_mock.side_effect = OSError('fail')
            self.assertRaises(OSError,
                              convert.execute, 'file:///test/path.raw')
            exc_mock.assert_called_once_with(
                'qemu-img', 'info',
                '-f', 'qcow2',
                '--output=json',
                '/test/path.raw',
                prlimit=async_utils.QEMU_IMG_PROC_LIMITS,
                python_exec=convert.python,
                log_errors=processutils.LOG_ALL_ERRORS)
        # Make sure we did not update the image
        self.img_repo.save.assert_not_called()

    def test_image_convert_inspection_reports_error(self):
        convert = self._setup_image_convert_info_fail()
        with mock.patch.object(processutils, 'execute') as exc_mock:
            exc_mock.return_value = '', 'some error'
            inspector = self.detect_file_format_mock.return_value
            inspector.__str__.return_value = 'qcow2'
            inspector.safety_check.return_value = True
            self.assertRaises(RuntimeError,
                              convert.execute, 'file:///test/path.raw')
            exc_mock.assert_called_once_with(
                'qemu-img', 'info',
                '-f', 'qcow2',
                '--output=json',
                '/test/path.raw',
                prlimit=async_utils.QEMU_IMG_PROC_LIMITS,
                python_exec=convert.python,
                log_errors=processutils.LOG_ALL_ERRORS)
        # Make sure we did not update the image
        self.img_repo.save.assert_not_called()

    def test_image_convert_invalid_qcow(self):
        data = {'format': 'qcow2',
                'backing-filename': '/etc/hosts'}

        convert = self._setup_image_convert_info_fail()
        with mock.patch.object(processutils, 'execute') as exc_mock:
            inspector = self.detect_file_format_mock.return_value
            inspector.__str__.return_value = 'qcow2'
            inspector.safety_check.return_value = True
            exc_mock.return_value = json.dumps(data), ''
            e = self.assertRaises(RuntimeError,
                                  convert.execute, 'file:///test/path.qcow')
            self.assertEqual('QCOW images with backing files are not allowed',
                             str(e))

    def test_image_convert_invalid_qcow_data_file(self):
        data = {'format': 'qcow2',
                'format-specific': {
                    'data': {
                        'data-file': '/etc/hosts',
                    },
                }}

        convert = self._setup_image_convert_info_fail()
        with mock.patch.object(processutils, 'execute') as exc_mock:
            exc_mock.return_value = json.dumps(data), ''
            inspector = self.detect_file_format_mock.return_value
            inspector.__str__.return_value = 'qcow2'
            inspector.safety_check.return_value = True
            e = self.assertRaises(RuntimeError,
                                  convert.execute, 'file:///test/path.qcow')
            self.assertEqual('QCOW images with data-file set are not allowed',
                             str(e))

    def test_image_convert_no_inspector_match(self):
        convert = self._setup_image_convert_info_fail()
        self.assertRaisesRegex(RuntimeError,
                               'Image format mismatch',
                               convert.execute, 'file:///test/path.hpfs')

    def test_image_convert_fails_inspection_safety_check(self):
        convert = self._setup_image_convert_info_fail()
        inspector = self.detect_file_format_mock.return_value
        inspector.safety_check.return_value = False
        self.assertRaisesRegex(RuntimeError,
                               'Image has disallowed configuration',
                               convert.execute, 'file:///test/path.qcow')

    def test_image_convert_fails_inspection_format_check(self):
        convert = self._setup_image_convert_info_fail()
        self.detect_file_format_mock.side_effect = (
            format_inspector.ImageFormatError())
        self.assertRaisesRegex(RuntimeError,
                               'Image format detection failed',
                               convert.execute, 'file:///test/path.qcow')

    def test_image_convert_fails_inspection_error(self):
        convert = self._setup_image_convert_info_fail()
        self.detect_file_format_mock.side_effect = ValueError
        self.assertRaisesRegex(RuntimeError,
                               'Unable to inspect image',
                               convert.execute, 'file:///test/path.qcow')

    def _test_image_convert_invalid_vmdk(self):
        data = {'format': 'vmdk',
                'format-specific': {
                    'data': {
                        'create-type': 'monolithicFlat',
                    }}}

        convert = self._setup_image_convert_info_fail(disk_format='vmdk')
        with mock.patch.object(processutils, 'execute') as exc_mock:
            inspector = self.detect_file_format_mock.return_value
            inspector.__str__.return_value = 'vmdk'
            inspector.safety_check.return_value = True
            exc_mock.return_value = json.dumps(data), ''
            convert.execute('file:///test/path.vmdk')

    def test_image_convert_invalid_vmdk(self):
        e = self.assertRaises(RuntimeError,
                              self._test_image_convert_invalid_vmdk)
        self.assertEqual('Invalid VMDK create-type specified', str(e))

    def test_image_convert_valid_vmdk_no_types(self):
        with mock.patch.object(CONF.image_format, 'vmdk_allowed_types',
                               new=[]):
            # We make it past the VMDK check and fail because our file
            # does not exist
            e = self.assertRaises(RuntimeError,
                                  self._test_image_convert_invalid_vmdk)
            self.assertEqual('Image is a VMDK, but no VMDK createType is '
                             'specified', str(e))

    def test_image_convert_valid_vmdk(self):
        with mock.patch.object(CONF.image_format, 'vmdk_allowed_types',
                               new=['monolithicSparse', 'monolithicFlat']):
            # We make it past the VMDK check and fail because our file
            # does not exist
            self.assertRaises(FileNotFoundError,
                              self._test_image_convert_invalid_vmdk)

    def test_image_convert_fails(self):
        convert = self._setup_image_convert_info_fail(disk_format='raw')
        with mock.patch.object(processutils, 'execute') as exc_mock:
            inspector = self.detect_file_format_mock.return_value
            inspector.__str__.return_value = 'raw'
            inspector.safety_check.return_value = True
            exc_mock.side_effect = [('{"format":"raw"}', ''),
                                    OSError('convert_fail')]
            self.assertRaises(OSError,
                              convert.execute, 'file:///test/path.raw')
            exc_mock.assert_has_calls(
                [mock.call('qemu-img', 'info',
                           '-f', 'raw',
                           '--output=json',
                           '/test/path.raw',
                           prlimit=async_utils.QEMU_IMG_PROC_LIMITS,
                           python_exec=convert.python,
                           log_errors=processutils.LOG_ALL_ERRORS),
                 mock.call('qemu-img', 'convert', '-f', 'raw', '-O', 'qcow2',
                           '/test/path.raw', '/test/path.raw.qcow2',
                           log_errors=processutils.LOG_ALL_ERRORS)])
        # Make sure we did not update the image
        self.img_repo.save.assert_not_called()

    def test_image_convert_reports_fail(self):
        convert = self._setup_image_convert_info_fail(disk_format='raw')
        with mock.patch.object(processutils, 'execute') as exc_mock:
            exc_mock.side_effect = [('{"format":"raw"}', ''),
                                    ('', 'some error')]
            inspector = self.detect_file_format_mock.return_value
            inspector.__str__.return_value = 'raw'
            inspector.safety_check.return_value = True
            self.assertRaises(RuntimeError,
                              convert.execute, 'file:///test/path.raw')
            exc_mock.assert_has_calls(
                [mock.call('qemu-img', 'info',
                           '-f', 'raw',
                           '--output=json',
                           '/test/path.raw',
                           prlimit=async_utils.QEMU_IMG_PROC_LIMITS,
                           python_exec=convert.python,
                           log_errors=processutils.LOG_ALL_ERRORS),
                 mock.call('qemu-img', 'convert', '-f', 'raw', '-O', 'qcow2',
                           '/test/path.raw', '/test/path.raw.qcow2',
                           log_errors=processutils.LOG_ALL_ERRORS)])
        # Make sure we did not update the image
        self.img_repo.save.assert_not_called()

    def test_image_convert_fails_source_format(self):
        convert = self._setup_image_convert_info_fail()
        with mock.patch.object(processutils, 'execute') as exc_mock:
            exc_mock.return_value = ('{}', '')
            inspector = self.detect_file_format_mock.return_value
            inspector.__str__.return_value = 'qcow2'
            inspector.safety_check.return_value = True
            exc = self.assertRaises(RuntimeError,
                                    convert.execute, 'file:///test/path.raw')
            self.assertIn('Image metadata disagrees about format', str(exc))
            exc_mock.assert_called_once_with(
                'qemu-img', 'info',
                '-f', 'qcow2',
                '--output=json',
                '/test/path.raw',
                prlimit=async_utils.QEMU_IMG_PROC_LIMITS,
                python_exec=convert.python,
                log_errors=processutils.LOG_ALL_ERRORS)
        # Make sure we did not update the image
        self.img_repo.save.assert_not_called()

    def test_image_convert_source_format_inspection_not_match(self):
        convert = self._setup_image_convert_info_fail(disk_format="raw")
        with mock.patch.object(processutils, 'execute') as exc_mock:
            exc_mock.return_value = (
                '{"format": "raw", "virtual-size": 123}', '')
            inspector = self.detect_file_format_mock.return_value
            inspector.__str__.return_value = 'qcow2'
            exc = self.assertRaises(RuntimeError,
                                    convert.execute, 'file:///test/path.raw')
            self.assertIn('Image format mismatch', str(exc))

    def test_image_convert_same_format_does_nothing(self):
        convert = self._setup_image_convert_info_fail()
        with mock.patch.object(processutils, 'execute') as exc_mock:
            exc_mock.return_value = (
                '{"format": "qcow2", "virtual-size": 123}', '')
            inspector = self.detect_file_format_mock.return_value
            inspector.__str__.return_value = 'qcow2'
            inspector.safety_check.return_value = True
            convert.execute('file:///test/path.qcow')
            # Make sure we only called qemu-img for inspection, not conversion
            exc_mock.assert_called_once_with(
                'qemu-img', 'info',
                '-f', 'qcow2',
                '--output=json',
                '/test/path.qcow',
                prlimit=async_utils.QEMU_IMG_PROC_LIMITS,
                python_exec=convert.python,
                log_errors=processutils.LOG_ALL_ERRORS)

        # Make sure we set the virtual_size before we exited
        image = self.img_repo.get.return_value
        self.assertEqual(123, image.virtual_size)

    def _set_image_conversion(self, mock_os_remove, stores=[]):
        mock_os_remove.return_value = None
        wrapper = mock.MagicMock()
        image_convert = image_conversion._ConvertImage(self.context,
                                                       self.task.task_id,
                                                       self.task_type,
                                                       wrapper,
                                                       stores)
        action = wrapper.__enter__.return_value
        self.task_repo.get.return_value = self.task
        return action, image_convert

    @mock.patch.object(os, 'remove')
    def test_image_convert_revert_success_multiple_stores(
            self, mock_os_remove):
        action, image_convert = self._set_image_conversion(
            mock_os_remove, stores=self.stores)

        with mock.patch.object(processutils, 'execute') as exc_mock:
            exc_mock.return_value = ("", None)
            with mock.patch.object(os.path, 'exists') as os_exists_mock:
                os_exists_mock.return_value = True
                image_convert.revert(result=mock.MagicMock())
                self.assertEqual(1, mock_os_remove.call_count)
                action.set_image_attribute.assert_called_once_with(
                    status='queued')
                action.remove_importing_stores.assert_called_once_with(
                    self.stores)
                action.add_failed_stores.assert_called_once_with(
                    self.stores)

    @mock.patch.object(os, 'remove')
    def test_image_convert_revert_success_single_store(
            self, mock_os_remove):
        action, image_convert = self._set_image_conversion(mock_os_remove)

        with mock.patch.object(processutils, 'execute') as exc_mock:
            exc_mock.return_value = ("", None)
            with mock.patch.object(os.path, 'exists') as os_exists_mock:
                os_exists_mock.return_value = True
                image_convert.revert(result=mock.MagicMock())
                self.assertEqual(1, mock_os_remove.call_count)
                self.assertEqual(0, action.remove_importing_stores.call_count)
                self.assertEqual(0, action.add_failed_store.call_count)
                action.set_image_attribute.assert_called_once_with(
                    status='queued')

    @mock.patch.object(os, 'remove')
    def test_image_convert_revert_success_src_file_exists(
            self, mock_os_remove):
        action, image_convert = self._set_image_conversion(
            mock_os_remove, stores=self.stores)
        image_convert.src_path = mock.MagicMock()

        with mock.patch.object(processutils, 'execute') as exc_mock:
            exc_mock.return_value = ("", None)
            with mock.patch.object(os.path, 'exists') as os_exists_mock:
                os_exists_mock.return_value = True
                image_convert.revert(result=mock.MagicMock())
                action.set_image_attribute.assert_called_once_with(
                    status='queued')
                action.remove_importing_stores.assert_called_once_with(
                    self.stores)
                action.add_failed_stores.assert_called_once_with(
                    self.stores)
                self.assertEqual(2, mock_os_remove.call_count)

    def test_image_convert_interpreter_configured(self):
        # By default, wsgi.python_interpreter is None; if it is
        # overridden, we should take the interpreter from config.
        fake_interpreter = '/usr/bin/python2.7'
        self.config(python_interpreter=fake_interpreter,
                    group='wsgi')
        convert = image_conversion._ConvertImage(self.context,
                                                 self.task.task_id,
                                                 self.task_type,
                                                 self.wrapper,
                                                 self.stores)
        self.assertEqual(fake_interpreter, convert.python)
