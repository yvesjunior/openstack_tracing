# Copyright (c) 2013 - 2015 EMC Corporation.
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

import json
import urllib

from cinder import context
from cinder import exception
from cinder.tests.unit import fake_volume
from cinder.tests.unit.volume.drivers.emc import scaleio
from cinder.tests.unit.volume.drivers.emc.scaleio import mocks


class TestCreateClonedVolume(scaleio.TestScaleIODriver):
    """Test cases for ``ScaleIODriver.create_cloned_volume()``"""
    def setUp(self):
        """Setup a test case environment.

        Creates fake volume objects and sets up the required API responses.
        """
        super(TestCreateClonedVolume, self).setUp()
        ctx = context.RequestContext('fake', 'fake', auth_token=True)

        self.src_volume = fake_volume.fake_volume_obj(
            ctx, **{'provider_id': 'pid001'})

        self.src_volume_name_2x_enc = urllib.quote(
            urllib.quote(
                self.driver._id_to_base64(self.src_volume.id)
            )
        )

        self.new_volume_extras = {
            'volumeIdList': ['cloned'],
            'snapshotGroupId': 'cloned_snapshot'
        }

        self.new_volume = fake_volume.fake_volume_obj(
            ctx, **self.new_volume_extras
        )

        self.new_volume_name_2x_enc = urllib.quote(
            urllib.quote(
                self.driver._id_to_base64(self.new_volume.id)
            )
        )
        self.HTTPS_MOCK_RESPONSES = {
            self.RESPONSE_MODE.Valid: {
                'types/Volume/instances/getByName::' +
                self.src_volume_name_2x_enc: self.src_volume.id,
                'instances/System/action/snapshotVolumes': '{}'.format(
                    json.dumps(self.new_volume_extras)),
            },
            self.RESPONSE_MODE.BadStatus: {
                'instances/System/action/snapshotVolumes':
                    self.BAD_STATUS_RESPONSE,
                'types/Volume/instances/getByName::' +
                    self.src_volume['provider_id']: self.BAD_STATUS_RESPONSE,
            },
            self.RESPONSE_MODE.Invalid: {
                'types/Volume/instances/getByName::' +
                    self.src_volume_name_2x_enc: None,
                'instances/System/action/snapshotVolumes':
                    mocks.MockHTTPSResponse(
                        {
                            'errorCode': 400,
                            'message': 'Invalid Volume Snapshot Test'
                        }, 400
                    ),
            },
        }

    def test_bad_login(self):
        self.set_https_response_mode(self.RESPONSE_MODE.BadStatus)
        self.assertRaises(exception.VolumeBackendAPIException,
                          self.driver.create_cloned_volume,
                          self.new_volume, self.src_volume)

    def test_invalid_source_volume(self):
        self.set_https_response_mode(self.RESPONSE_MODE.Invalid)
        self.assertRaises(exception.VolumeBackendAPIException,
                          self.driver.create_cloned_volume,
                          self.new_volume, self.src_volume)

    def test_create_cloned_volume(self):
        self.set_https_response_mode(self.RESPONSE_MODE.Valid)
        self.driver.create_cloned_volume(self.new_volume, self.src_volume)
