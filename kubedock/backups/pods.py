
# KuberDock - is a platform that allows users to run applications using Docker
# container images and create SaaS / PaaS based on these applications.
# Copyright (C) 2017 Cloud Linux INC
#
# This file is part of KuberDock.
#
# KuberDock is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# KuberDock is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with KuberDock; if not, see <http://www.gnu.org/licenses/>.

import os

from kubedock import validation
from kubedock.backups import utils
from kubedock.exceptions import APIError
from kubedock.kapi.pod import VolumeExists
from kubedock.kapi.podcollection import PodCollection
from kubedock.pods.models import Pod as DBPod, PersistentDisk, \
    PersistentDiskStatuses
from kubedock.users import User
from kubedock.utils import nested_dict_utils

DEFAULT_BACKUP_PATH_TEMPLATE = '/{owner_id}/{volume_name}.tar.gz'


def _filter_persistent_volumes(pod_spec):
    def is_local_storage(volume_spec):
        return bool(nested_dict_utils.get(volume_spec,
                                          'persistentDisk.pdName'))

    return [v for v in pod_spec['volumes'] if is_local_storage(v)]


class MultipleErrors(APIError):
    message = 'Multiple errors'

    def __init__(self, errors):
        details = {
            'errors': [
                {
                    'data': e.message,
                    'type': getattr(e, 'type', e.__class__.__name__),
                    'details': e.details
                }
                for e in errors
            ]
        }
        super(MultipleErrors, self).__init__(details=details)


class BackupUrlFactory(object):
    def __init__(self, base_url, path_template, **kwargs):
        self.base_url = base_url
        self.path_template = path_template
        self.kwargs = kwargs

    def get_url(self, volume_name, volume_path=None):
        if volume_path is not None:
            volume_name = os.path.basename(volume_path)

        path = self.path_template.format(
            volume_name=volume_name, **self.kwargs)
        return utils.join_url(self.base_url, path)


class _PodRestoreCommand(object):
    def __init__(self, pod_dump, owner, pv_backups_location,
                 pv_backups_path_template):
        self.pod_dump = pod_dump
        self.owner = owner
        self.pv_backups_location = pv_backups_location
        if pv_backups_path_template is None:
            pv_backups_path_template = DEFAULT_BACKUP_PATH_TEMPLATE

        template_dict = {
            'owner_id': owner.id,
            'owner_name': owner.username,
            'original_owner_id': nested_dict_utils.get(pod_dump, 'owner.id'),
            'original_owner_name': nested_dict_utils.get(
                pod_dump, 'owner.username'),
        }

        self.backup_url_factory = BackupUrlFactory(
            pv_backups_location, pv_backups_path_template, **template_dict)

    def __call__(self):
        pod_dump = self.pod_dump
        validation.check_pod_dump(pod_dump, user=self.owner,
                                  allow_unknown=True)

        pod_data = pod_dump['pod_data']
        volumes_map = pod_dump['volumes_map']
        persistent_volumes = _filter_persistent_volumes(pod_data)
        # we have to restore all specified pv.
        # may be it will be changed later.
        pv_restore_needed = bool(persistent_volumes)  # if pv list is not empty
        if pv_restore_needed:
            if (not self.pv_backups_location and
                any(vol != 'ceph' for vol in volumes_map.values())):
                raise APIError('POD spec contains persistent volumes '
                               'but backups location was not specified')
            self._extend_pv_specs_with_backup_info(persistent_volumes,
                                                   volumes_map)

        self._check_for_conflicts(pod_data, volumes_map)

        restored_pod_dict = self._restore_pod(pod_dump)
        restored_pod_dict = self._start_pod_if_needed(restored_pod_dict)
        return restored_pod_dict

    def _extend_pv_specs_with_backup_info(self, pv_specs, volumes_map):
        for pv_spec in pv_specs:
            pd_name = nested_dict_utils.get(pv_spec, 'name')
            pd_path = volumes_map[pd_name]
            if pd_path == 'ceph':
                return
            backup_url = self.backup_url_factory.get_url(pd_name, pd_path)
            nested_dict_utils.set(pv_spec, 'annotation.backupUrl', backup_url)

    def _check_for_conflicts(self, pod_data, volumes_map):
        errors = []
        pod_name = nested_dict_utils.get(pod_data, 'name')
        e = self._check_pod_name(pod_name)
        if e:
            errors.append(e)

        vols = _filter_persistent_volumes(pod_data)
        for vol in vols:
            volume_name = nested_dict_utils.get(vol, 'persistentDisk.pdName')
            if volumes_map.get(vol.get('name')) != 'ceph':
                e = self._check_volume_name(volume_name)
                if e:
                    errors.append(e)
        if errors:
            if len(errors) == 1:
                raise errors[0]
            else:
                raise MultipleErrors(errors)

    def _check_pod_name(self, pod_name):
        pod = DBPod.query.filter(
            DBPod.name == pod_name, DBPod.owner_id == self.owner.id
        ).first()
        if pod:
            return APIError(
                'Pod with name "{0}" already exists.'.format(pod_name),
                status_code=409, type='PodNameConflict',
                details={'id': pod.id, 'name': pod.name}
            )

    def _check_volume_name(self, volume_name):
        persistent_disk = PersistentDisk.filter(
            PersistentDisk.owner_id == self.owner.id,
            PersistentDisk.name == volume_name,
            PersistentDisk.state.in_([
                PersistentDiskStatuses.PENDING,
                PersistentDiskStatuses.CREATED
            ])
        ).first()
        if persistent_disk:
            return VolumeExists(persistent_disk.name, persistent_disk.id)

    def _restore_pod(self, pod_dump):
        pod_collection = PodCollection(owner=self.owner)
        restored_pod_dict = pod_collection.add_from_dump(pod_dump)
        return restored_pod_dict

    def _start_pod_if_needed(self, restored_pod_dict):
        saved_status = self.pod_dump['pod_data']['status']

        if saved_status == 'running':
            restored_pod_id = restored_pod_dict['id']
            restored_pod_dict = PodCollection(owner=self.owner).update(
                pod_id=restored_pod_id, data={'command': 'start'}
            )
        return restored_pod_dict


def restore(pod_dump, owner, pv_backups_location=None,
            pv_backups_path_template=None):
    """Restore pod from backup.

    Args:
        pod_dump (dict): Pod dump.
        owner (User): Pod's owner.
        pv_backups_location (str): Url where backups are stored.
        pv_backups_path_template (str): Template of path to backup at backups
            location. Standard python template in form of
            'some text with {some_key}'.
            Available keys are:
                owner_id, owner_name, original_owner_id, original_owner_name,
                volume_name.
            Default template is specified in `DEFAULT_BACKUP_PATH_TEMPLATE`.

    Returns:
        dict: Dictionary with restored pod's data.

    Notes:
        Expected that pod_dump contains data got from `/api/dump/pods`.
    """
    return _PodRestoreCommand(
        pod_dump, owner, pv_backups_location, pv_backups_path_template,
    )()
