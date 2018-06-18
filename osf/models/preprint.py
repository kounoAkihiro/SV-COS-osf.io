# -*- coding: utf-8 -*-
import functools
import urlparse
import logging
import re
import pytz

from dirtyfields import DirtyFieldsMixin
from django.apps import apps
from django.db import models
from django.utils import timezone
from django.contrib.contenttypes.fields import GenericRelation

from framework.auth import Auth
from framework.postcommit_tasks.handlers import enqueue_postcommit_task
from framework.exceptions import PermissionsError
from framework.analytics import increment_user_activity_counters

from osf.models import Subject, Tag, OSFUser
from osf.models.preprintlog import PreprintLog
from osf.models.contributor import PreprintContributor
from osf.models.mixins import ReviewableMixin, Taggable, Loggable, GuardianMixin
from osf.models.validators import validate_subject_hierarchy, validate_title, validate_doi
from osf.utils.fields import NonNaiveDateTimeField
from osf.utils.workflows import DefaultStates
from osf.utils import sanitize
from osf.utils.requests import DummyRequest, get_request_and_user_id, get_headers_from_request
from website.notifications.emails import get_user_subscriptions
from website.notifications import utils
from website.preprints.tasks import on_preprint_updated
from website.project.licenses import set_license
from website.util import api_v2_url, api_url_for, web_url_for
from website.citations.utils import datetime_to_csl
from website import settings, mails
from website.project import signals as project_signals

from osf.models.base import BaseModel, GuidMixin
from osf.models.identifiers import IdentifierMixin, Identifier
from osf.models.mixins import TaxonomizableMixin, ContributorMixin, SpamOverrideMixin
from addons.osfstorage.mixins import UploadMixin
from addons.osfstorage.models import OsfStorageFolder, Region

from framework.sentry import log_exception
from osf.exceptions import (
    PreprintStateError, InvalidTagError, TagNotFoundError
)

logger = logging.getLogger(__name__)


class Preprint(DirtyFieldsMixin, GuidMixin, IdentifierMixin, ReviewableMixin, UploadMixin,
        BaseModel, Loggable, Taggable, GuardianMixin, SpamOverrideMixin, TaxonomizableMixin, ContributorMixin):
    # Preprint fields that trigger a check to the spam filter on save
    SPAM_CHECK_FIELDS = {
        'title',
        'description',
    }

    # Setting for ContributorMixin
    DEFAULT_CONTRIBUTOR_PERMISSIONS = 'write'

    provider = models.ForeignKey('osf.PreprintProvider',
                                 on_delete=models.SET_NULL,
                                 related_name='preprints',
                                 null=True, blank=True, db_index=True)
    node = models.ForeignKey('osf.AbstractNode', on_delete=models.SET_NULL,
                             related_name='preprints',
                             null=True, blank=True, db_index=True)
    is_published = models.BooleanField(default=False, db_index=True)
    date_published = NonNaiveDateTimeField(null=True, blank=True)
    original_publication_date = NonNaiveDateTimeField(null=True, blank=True)
    license = models.ForeignKey('osf.NodeLicenseRecord',
                                on_delete=models.SET_NULL, null=True, blank=True)

    identifiers = GenericRelation(Identifier, related_query_name='preprints')
    preprint_doi_created = NonNaiveDateTimeField(default=None, null=True, blank=True)
    # begin changes
    title = models.TextField(
        validators=[validate_title]
    )  # this should be a charfield but data from mongo didn't fit in 255
    description = models.TextField(blank=True, default='')
    creator = models.ForeignKey(OSFUser,
                                db_index=True,
                                related_name='preprints_created',
                                on_delete=models.SET_NULL,
                                null=True, blank=True)
    _contributors = models.ManyToManyField(OSFUser,
                                           through=PreprintContributor,
                                           related_name='preprints')
    article_doi = models.CharField(max_length=128,
                                            validators=[validate_doi],
                                            null=True, blank=True)
    files = GenericRelation('osf.OsfStorageFile', object_id_field='target_object_id', content_type_field='target_content_type')
    primary_file = models.ForeignKey('osf.OsfStorageFile', null=True, blank=True, related_name='preprint')
    # (for legacy preprints), pull off of node
    is_public = models.BooleanField(default=True, db_index=True)
    # Datetime when old node was deleted (for legacy preprints)
    deleted = NonNaiveDateTimeField(null=True, blank=True)
    # For legacy preprints
    migrated = NonNaiveDateTimeField(null=True, blank=True)
    region = models.ForeignKey(Region, null=True, blank=True, on_delete=models.CASCADE)
    groups = {
        'read': ('read_preprint',),
        'write': ('read_preprint', 'write_preprint',),
        'admin': ('read_preprint', 'write_preprint', 'admin_preprint',)
    }
    group_format = 'preprint_{self.id}_{group}'

    class Meta:
        permissions = (
            ('osf_admin_view_preprint', 'Can view preprint details in the admin app.'),
            ('read_preprint', 'Can read the preprint'),
            ('write_preprint', 'Can write the preprint'),
            ('admin_preprint', 'Can manage the preprint'),
        )

    def __unicode__(self):
        return '{} ({} preprint) (guid={}){}'.format(self.title, 'published' if self.is_published else 'unpublished', self._id, ' with supplemental files on ' + self.node.__unicode__() if self.node else '')

    @property
    def contributors(self):
        # Overrides ContributorMixin
        # NOTE: _order field is generated by order_with_respect_to = 'preprint'
        return self._contributors.order_by('preprintcontributor___order')

    @property
    def visible_contributors(self):
        # Overrides ContributorMixin
        return OSFUser.objects.filter(
            preprintcontributor__preprint=self,
            preprintcontributor__visible=True
        ).order_by('preprintcontributor___order')

    @property
    def log_class(self):
        # Property needed for ContributorMixin
        return PreprintLog

    @property
    def contributor_class(self):
        # Property needed for ContributorMixin
        return PreprintContributor

    @property
    def contributor_kwargs(self):
        # Property needed for ContributorMixin
        return {
            'preprint': self
        }

    @property
    def log_params(self):
        # Property needed for ContributorMixin
        return {
            'preprint': self._id
        }

    @property
    def contributor_set(self):
        # Property needed for ContributorMixin
        return self.preprintcontributor_set

    @property
    def verified_publishable(self):
        return self.is_published and \
            self.is_public and \
            self.has_submitted_preprint and not \
            self.deleted and not \
            self.is_preprint_orphan

    @property
    def preprint_doi(self):
        return self.get_identifier_value('doi')

    @property
    def is_preprint_orphan(self):
        if not self.primary_file_id or self.primary_file.deleted_on or self.primary_file.target != self:
            return True
        return False

    @property
    def has_submitted_preprint(self):
        return self.machine_state != DefaultStates.INITIAL.value

    @property
    def deep_url(self):
        # Required for GUID routing
        return '/preprints/{}/'.format(self._id)

    @property
    def url(self):
        if (self.provider.domain_redirect_enabled and self.provider.domain) or self.provider._id == 'osf':
            return '/{}/'.format(self._id)

        return '/preprints/{}/{}/'.format(self.provider._id, self._id)

    @property
    def absolute_url(self):
        return urlparse.urljoin(
            self.provider.domain if self.provider.domain_redirect_enabled else settings.DOMAIN,
            self.url
        )

    @property
    def absolute_api_v2_url(self):
        path = '/preprints/{}/'.format(self._id)
        return api_v2_url(path)

    @property
    def display_absolute_url(self):
        url = self.absolute_url
        if url is not None:
            return re.sub(r'https?:', '', url).strip('/')

    @property
    def admin_contributor_ids(self):
        # Overrides ContributorMixin
        return self.get_group('admin').user_set.filter(is_active=True).values_list('guids___id', flat=True)

    @property
    def csl(self):  # formats node information into CSL format for citation parsing
        """a dict in CSL-JSON schema

        For details on this schema, see:
            https://github.com/citation-style-language/schema#csl-json-schema
        """
        csl = {
            'id': self._id,
            'title': sanitize.unescape_entities(self.title),
            'author': [
                contributor.csl_name(self._id)  # method in auth/model.py which parses the names of authors
                for contributor in self.visible_contributors
            ],
            'publisher': 'Open Science Framework',
            'type': 'webpage',
            'URL': self.display_absolute_url,
            'publisher': self.provider.name,
        }

        article_doi = self.article_doi
        preprint_doi = self.preprint_doi

        if article_doi:
            csl['DOI'] = article_doi
        elif preprint_doi and self.is_published and self.preprint_doi_created:
            csl['DOI'] = preprint_doi

        if self.logs.exists():
            csl['issued'] = datetime_to_csl(self.logs.latest().created)

        if self.original_publication_date:
            csl['issued'] = datetime_to_csl(self.original_publication_date)

        return csl

    def web_url_for(self, view_name, _absolute=False, _guid=False, *args, **kwargs):
        return web_url_for(view_name, pid=self._id,
                           _absolute=_absolute, _guid=_guid, *args, **kwargs)

    def api_url_for(self, view_name, _absolute=False, *args, **kwargs):
        return api_url_for(view_name, pid=self._id, _absolute=_absolute, *args, **kwargs)

    def get_absolute_url(self):
        return self.absolute_api_v2_url

    def add_log(self, action, params, auth, foreign_user=None, log_date=None, save=True, request=None):
        user = None
        if auth:
            user = auth.user
        elif request:
            user = request.user

        params['preprint'] = params.get('preprint') or self._id

        log = PreprintLog(
            action=action, user=user, foreign_user=foreign_user,
            params=params, preprint=self
        )

        log.save()

        if self.logs.count() == 1:
            self.last_logged = log.created.replace(tzinfo=pytz.utc)
        else:
            self.last_logged = self.logs.first().created

        if save:
            self.save()
        if user:
            increment_user_activity_counters(user._primary_key, action, log.created.isoformat())

        return log

    # Overrides ContributorMixin
    def has_permission(self, user, permission):
        """Check whether user has permission.
        :param User user: User to test
        :param str permission: Required permission
        :returns: User has required permission
        """
        if not user:
            return False
        return user.has_perm('{}_preprint'.format(permission), self)

    # Overrides ContributorMixin
    def set_permissions(self, user, permissions, validate=True, save=False):
        # Ensure that user's permissions cannot be lowered if they are the only admin
        if isinstance(user, PreprintContributor):
            user = user.user

        if validate and (self.has_permission(user, 'admin') and 'admin' not in permissions):
            if self.get_group('admin').user_set.count() <= 1:
                raise PreprintStateError('Must have at least one registered admin contributor')
        self.clear_permissions(user)
        self.add_permission(user, permissions)
        if save:
            self.save()

    def get_subjects(self):
        ret = []
        for subj_list in self.subject_hierarchy:
            subj_hierarchy = []
            for subj in subj_list:
                if subj:
                    subj_hierarchy += ({'id': subj._id, 'text': subj.text}, )
            if subj_hierarchy:
                ret.append(subj_hierarchy)
        return ret

    def set_subjects(self, preprint_subjects, auth, log=True):
        if not self.has_permission(auth.user, 'write'):
            raise PermissionsError('Must have admin or write permissions to change a preprint\'s subjects.')

        old_subjects = list(self.subjects.values_list('id', flat=True))
        self.subjects.clear()
        for subj_list in preprint_subjects:
            subj_hierarchy = []
            for s in subj_list:
                subj_hierarchy.append(s)
            if subj_hierarchy:
                validate_subject_hierarchy(subj_hierarchy)
                for s_id in subj_hierarchy:
                    self.subjects.add(Subject.load(s_id))

        if log:
            self.add_log(
                action=PreprintLog.SUBJECTS_UPDATED,
                params={
                    'subjects': list(self.subjects.values('_id', 'text')),
                    'old_subjects': list(Subject.objects.filter(id__in=old_subjects).values('_id', 'text')),
                    'preprint': self._id
                },
                auth=auth,
                save=False,
            )

        self.save(old_subjects=old_subjects)

    def set_primary_file(self, preprint_file, auth, save=False):
        # TODO might have to rework. Do we need osfstorage checks?

        if not self.root_folder:
            raise PreprintStateError('Preprint needs a root folder.')

        if not self.has_permission(auth.user, 'write'):
            raise PermissionsError('Must have admin or write permissions to change a preprint\'s primary file.')

        if preprint_file.target != self or preprint_file.provider != 'osfstorage':
            raise ValueError('This file is not a valid primary file for this preprint.')

        existing_file = self.primary_file
        self.primary_file = preprint_file

        self.primary_file.move_under(self.root_folder)
        self.primary_file.save()

        # only log if updating the preprint file, not adding for the first time
        if existing_file:
            self.add_log(
                action=PreprintLog.FILE_UPDATED,
                params={
                    'preprint': self._id,
                    'file': self.primary_file._id
                },
                auth=auth,
                save=False
            )

        if save:
            self.save()
        self.update_search()

    def set_published(self, published, auth, save=False):
        if not self.has_permission(auth.user, 'admin'):
            raise PermissionsError('Only admins can publish a preprint.')

        if self.is_published and not published:
            raise ValueError('Cannot unpublish preprint.')

        self.is_published = published

        if published:
            if not self.title:
                raise ValueError('Preprint needs a title; cannot publish.')
            if not (self.primary_file and self.primary_file.target == self):
                raise ValueError('Preprint is not a valid preprint; cannot publish.')
            if not self.provider:
                raise ValueError('Preprint provider not specified; cannot publish.')
            if not self.subjects.exists():
                raise ValueError('Preprint must have at least one subject to be published.')
            self.date_published = timezone.now()
            # For legacy preprints, not logging
            self.set_privacy('public', log=False, save=False)

            # In case this provider is ever set up to use a reviews workflow, put this preprint in a sensible state
            self.machine_state = DefaultStates.ACCEPTED.value
            self.date_last_transitioned = self.date_published

            self.add_log(
                action=PreprintLog.PUBLISHED,
                params={
                    'preprint': self._id
                },
                auth=auth,
                save=False,
            )
            self._send_preprint_confirmation(auth)

        if save:
            self.save()
        self.update_search()

    def set_preprint_license(self, license_detail, auth, save=False):
        license_record, license_changed = set_license(self, license_detail, auth, node_type='preprint')

        if license_changed:
            self.add_log(
                action=PreprintLog.CHANGED_LICENSE,
                params={
                    'preprint': self._id,
                    'new_license': license_record.node_license.name
                },
                auth=auth,
                save=False
            )

        if save:
            self.save()
        self.update_search()

    def set_identifier_values(self, doi, save=False):
        self.set_identifier_value('doi', doi)
        self.preprint_doi_created = timezone.now()

        if save:
            self.save()

    def save(self, *args, **kwargs):
        first_save = not bool(self.pk)
        saved_fields = self.get_dirty_fields() or []
        old_subjects = kwargs.pop('old_subjects', [])
        ret = super(Preprint, self).save(*args, **kwargs)
        if (not first_save and 'is_published' in saved_fields) or self.is_published:
            enqueue_postcommit_task(on_preprint_updated, (self._id,), {'old_subjects': old_subjects}, celery=True)

        if saved_fields:
            self.on_update(first_save, saved_fields)

        if first_save:
            self._set_default_region()
            self.update_group_permissions()

            self.add_log(
                action=PreprintLog.CREATED,
                params={
                    'preprint': self._id
                },
                auth=Auth(user=self.creator),
                save=False,
            )
            self._add_creator_as_contributor()
        return ret

    def _set_default_region(self):
        user_settings = self.creator.get_addon('osfstorage')
        self.region_id = user_settings.default_region_id
        self.save()

    def _add_creator_as_contributor(self):
        self.add_contributor(self.creator, permissions='admin', visible=True, log=False, save=True)

    def _send_preprint_confirmation(self, auth):
        # Send creator confirmation email
        recipient = self.creator
        event_type = utils.find_subscription_type('global_reviews')
        user_subscriptions = get_user_subscriptions(recipient, event_type)
        if self.provider._id == 'osf':
            logo = settings.OSF_PREPRINTS_LOGO
        else:
            logo = self.provider._id

        context = {
            'domain': settings.DOMAIN,
            'reviewable': self,
            'workflow': self.provider.reviews_workflow,
            'provider_url': '{domain}preprints/{provider_id}'.format(
                            domain=self.provider.domain or settings.DOMAIN,
                            provider_id=self.provider._id if not self.provider.domain else '').strip('/'),
            'provider_contact_email': self.provider.email_contact or settings.OSF_CONTACT_EMAIL,
            'provider_support_email': self.provider.email_support or settings.OSF_SUPPORT_EMAIL,
            'no_future_emails': user_subscriptions['none'],
            'is_creator': True,
            'provider_name': 'OSF Preprints' if self.provider.name == 'Open Science Framework' else self.provider.name,
            'logo': logo,
        }

        mails.send_mail(
            recipient.username,
            mails.REVIEWS_SUBMISSION_CONFIRMATION,
            mimetype='html',
            user=recipient,
            **context
        )

    # FOLLOWING BEHAVIOR NOT SPECIFIC TO PREPRINTS

    @property
    def all_tags(self):
        """Return a queryset containing all of this node's tags (incl. system tags)."""
        # Tag's default manager only returns non-system tags, so we can't use self.tags
        return Tag.all_tags.filter(preprint_tagged=self)

    @property
    def system_tags(self):
        """The system tags associated with this node. This currently returns a list of string
        names for the tags, for compatibility with v1. Eventually, we can just return the
        QuerySet.
        """
        return self.all_tags.filter(system=True).values_list('name', flat=True)

    # Override Taggable
    def add_tag_log(self, tag, auth):
        self.add_log(
            action=PreprintLog.TAG_ADDED,
            params={
                'preprint': self._id,
                'tag': tag.name
            },
            auth=auth,
            save=False
        )

    # Override Taggable
    def on_tag_added(self, tag):
        self.update_search()

    def remove_tag(self, tag, auth, save=True):
        if not tag:
            raise InvalidTagError
        elif not self.tags.filter(name=tag).exists():
            raise TagNotFoundError
        else:
            tag_obj = Tag.objects.get(name=tag)
            self.tags.remove(tag_obj)
            self.add_log(
                action=PreprintLog.TAG_REMOVED,
                params={
                    'preprint': self._id,
                    'tag': tag,
                },
                auth=auth,
                save=False,
            )
            if save:
                self.save()
            self.update_search()
            return True

    def set_supplemental_node(self, node, auth, save=False):
        if not self.has_permission(auth.user, 'write'):
            raise PermissionsError('You must have write permissions to set a supplemental node.')

        if not node.has_permission(auth.user, 'write'):
            raise PermissionsError('You must have write permissions on the supplemental node to attach.')

        node_preprints = node.preprints.filter(provider=self.provider)
        if node_preprints.exists():
            raise ValueError('Only one preprint per provider can be submitted for a node. Check preprint `{}`.'.format(node_preprints.first()._id))

        if node.is_deleted:
            raise ValueError('Cannot attach a deleted project to a preprint.')

        self.node = node

        self.add_log(
            action=PreprintLog.SUPPLEMENTAL_NODE_ADDED,
            params={
                'preprint': self._id,
                'node': self.node._id,
            },
            auth=auth,
            save=False,
        )

        if save:
            self.save()

    def set_title(self, title, auth, save=False):
        """Set the title of this Node and log it.

        :param str title: The new title.
        :param auth: All the auth information including user, API key.
        """
        if not self.has_permission(auth.user, 'write'):
            raise PermissionsError('Must have admin or write permissions to edit a preprint\'s title.')

        # Called so validation does not have to wait until save.
        validate_title(title)

        original_title = self.title
        new_title = sanitize.strip_html(title)
        # Title hasn't changed after sanitzation, bail out
        if original_title == new_title:
            return False
        self.title = new_title
        self.add_log(
            action=PreprintLog.EDITED_TITLE,
            params={
                'preprint': self._id,
                'title_new': self.title,
                'title_original': original_title,
            },
            auth=auth,
            save=False,
        )
        if save:
            self.save()
        self.update_search()
        return None

    def set_description(self, description, auth, save=False):
        """Set the description and log the event.

        :param str description: The new description
        :param auth: All the auth informtion including user, API key.
        :param bool save: Save self after updating.
        """
        if not self.has_permission(auth.user, 'write'):
            raise PermissionsError('Must have admin or write permissions to edit a preprint\'s title.')

        original = self.description
        new_description = sanitize.strip_html(description)
        if original == new_description:
            return False
        self.description = new_description
        self.add_log(
            action=PreprintLog.EDITED_DESCRIPTION,
            params={
                'preprint': self._id,
                'description_new': self.description,
                'description_original': original
            },
            auth=auth,
            save=False,
        )
        if save:
            self.save()
        self.update_search()
        return None

    def set_privacy(self, permissions, auth=None, log=True, save=True):
        """Set the permissions for this preprint - mainly for spam purposes.

        :param permissions: A string, either 'public' or 'private'
        :param auth: All the auth information including user, API key.
        :param bool log: Whether to add a NodeLog for the privacy change.
        :param bool meeting_creation: Whether this was created due to a meetings email.
        :param bool check_addons: Check and collect messages for addons?
        """
        if auth and not self.has_permission(auth.user, 'write'):
            raise PermissionsError('Must have admin or write permissions to change privacy settings.')
        if permissions == 'public' and not self.is_public:
            if self.is_spam or (settings.SPAM_FLAGGED_MAKE_NODE_PRIVATE and self.is_spammy):
                # TODO: Should say will review within a certain agreed upon time period.
                raise PreprintStateError('This preprint has been marked as spam. Please contact the help desk if you think this is in error.')
            self.is_public = True
        elif permissions == 'private' and self.is_public:
            self.is_public = False
        else:
            return False

        if log:
            action = PreprintLog.MADE_PUBLIC if permissions == 'public' else PreprintLog.MADE_PRIVATE
            self.add_log(
                action=action,
                params={
                    'preprint': self._id,
                },
                auth=auth,
                save=False,
            )
        if save:
            self.save()
        self.update_search()
        return True

    def can_view(self, auth):
        if not auth.user:
            return self.verified_publishable

        return (self.verified_publishable or
            (self.is_public and auth.user.has_perm('view_submissions', self.provider)) or
            self.has_permission(auth.user, 'admin') or
            (self.is_contributor(auth.user) and self.has_submitted_preprint)
        )

    def can_edit(self, auth=None, user=None):
        """Return if a user is authorized to edit this preprint.
        Must specify one of (`auth`, `user`).

        :param Auth auth: Auth object to check
        :param User user: User object to check
        :returns: Whether user has permission to edit this node.
        """
        if not auth and not user:
            raise ValueError('Must pass either `auth` or `user`')
        if auth and user:
            raise ValueError('Cannot pass both `auth` and `user`')
        user = user or auth.user

        return (
            user and ((self.has_permission(user, 'write') and self.has_submitted_preprint) or self.has_permission(user, 'admin'))
        )

    # Overrides ContributorMixin entirely, since Preprints use guardian permissions
    def add_permission(self, user, permission, save=False):
        """Grant permission to a user.

        :param User user: User to grant permission to
        :param str permission: Permission to grant
        :param bool save: Save changes
        :raises: ValueError if user already has permission
        """
        permission_group = self.get_group(permission)

        if not permission_group.user_set.filter(id=user.id).exists():
            permission_group.user_set.add(user)
        else:
            raise ValueError('User already has permission {0}'.format(permission))
        if save:
            self.save()

    # Overrides ContributorMixin entirely, since Preprints use guardian permissions
    def remove_permission(self, user, permission, save=False):
        """Revoke permission from a user.

        :param User user: User to revoke permission from
        :param str permission: Permission to revoke
        :param bool save: Save changes
        :raises: ValueError if user does not have permission
        """
        permission_group = self.get_group(permission)

        if permission_group.user_set.filter(id=user.id).exists():
            permission_group.user_set.remove(user)
        else:
            raise ValueError('User does not have permission {0}'.format(permission))
        if save:
            self.save()

    def clear_permissions(self, user):
        for name in self.groups.keys():
            if user.groups.filter(name=self.get_group(name)).exists():
                self.remove_permission(user, name)

    def get_contributor_order(self):
        # Method needed for ContributorMixin
        return self.get_preprintcontributor_order()

    def set_contributor_order(self, contributor_ids):
        # Method needed for ContributorMixin
        return self.set_preprintcontributor_order(contributor_ids)

    # Overrides ContributorMixin send_email default
    def add_contributor(self, contributor, permissions=None, visible=True,
                        send_email='preprint', auth=None, log=True, save=False):
        contrib_to_add = super(Preprint, self).add_contributor(contributor=contributor, permissions=permissions, visible=visible, send_email=send_email, auth=auth, log=log, save=save)
        if self._id and self.is_published and contrib_to_add:
            project_signals.contributor_added.send(self,
                                                   contributor=contributor,
                                                   auth=auth, email_template=send_email)
        return contrib_to_add

    # Overrides ContributorMixin send_email default
    def add_unregistered_contributor(self, fullname, email, auth, send_email='preprint',
                                     visible=True, permissions=None, save=False, existing_user=None):
        return super(Preprint, self).add_unregistered_contributor(fullname=fullname, email=email, auth=auth, send_email=send_email, visible=visible, permissions=permissions, save=save, existing_user=existing_user)

    # Overrides replace_contributor since users needed to be added to groups
    def replace_contributor(self, old, new):
        res = super(Preprint, self).replace_contributor(old, new)

        for group_name in self.groups.keys():
            if self.get_group(group_name).user_set.filter(id=old.id).exists():
                self.get_group(group_name).user_set.remove(old)
                self.get_group(group_name).user_set.add(new)
        return res

    # Overrides ContributorMixin to run update_search
    def move_contributor(self, contributor, auth, index, save=False):
        super(Preprint, self).move_contributor(contributor, auth, index, save)
        self.update_search()

    # Overrides ContributorMixin since this query is constructed differently
    def _get_admin_contributors_query(self, users):
        return PreprintContributor.objects.select_related('user').filter(
            preprint=self,
            user__in=users,
            user__is_active=True,
            user__groups=(self.get_group('admin').id))

    # Overrides ContributorMixin to run update_search
    def set_visible(self, user, visible, log=True, auth=None, save=False):
        super(Preprint, self).set_visible(user, visible, log=log, auth=auth, save=save)
        self.update_search()

    def on_update(self, first_save, saved_fields):
        User = apps.get_model('osf.OSFUser')
        request, user_id = get_request_and_user_id()
        request_headers = {}
        if not isinstance(request, DummyRequest):
            request_headers = {
                k: v
                for k, v in get_headers_from_request(request).items()
                if isinstance(v, basestring)
            }

        user = User.load(user_id)
        if user and self.check_spam(user, saved_fields, request_headers):
            # Specifically call the super class save method to avoid recursion into model save method.
            super(Preprint, self).save()

    @classmethod
    def bulk_update_search(cls, preprints, index=None):
        from website import search
        try:
            serialize = functools.partial(search.search.update_preprint, index=index, bulk=True, async=False)
            search.search.bulk_update_nodes(serialize, preprints, index=index)
        except search.exceptions.SearchUnavailableError as e:
            logger.exception(e)
            log_exception()

    def update_search(self):
        from website import search
        try:
            search.search.update_preprint(self, bulk=False, async=True)
        except search.exceptions.SearchUnavailableError as e:
            logger.exception(e)
            log_exception()

    def create_root_folder(self):
        if self.root_folder:
            return self.root_folder

        # Note: The "root" node will always be "named" empty string
        root_folder = OsfStorageFolder(name='', target=self, is_root=True)
        root_folder.save()

        self.root_folder = root_folder
        self.save()
        return root_folder

    def serialize_waterbutler_settings(self):
        """
        Since preprints don't have addons, this method has been pulled over from the
        OSFStorage addon
        """
        root_folder = self.create_root_folder()
        return dict(Region.objects.get(id=self.region_id).waterbutler_settings, **{
            'nid': self._id,
            'rootId': root_folder._id,
            'baseUrl': api_url_for(
                'osfstorage_get_metadata',
                guid=self._id,
                _absolute=True,
                _internal=True
            )
        })

    def serialize_waterbutler_credentials(self):
        """
        Since preprints don't have addons, this method has been pulled over from the
        OSFStorage addon
        """
        return Region.objects.get(id=self.region_id).waterbutler_credentials

    def create_waterbutler_log(self, auth, action, metadata):
        """
        Since preprints don't have addons, this method has been pulled over from the
        OSFStorage addon
        """
        user = auth.user
        params = {
            'preprint': self._id,
            'path': metadata['materialized'],
        }
        if (metadata['kind'] != 'folder'):
            url = self.web_url_for(
                'addon_view_or_download_file',
                guid=self._id,
                path=metadata['path'],
                provider='osfstorage'
            )
            params['urls'] = {'view': url, 'download': url + '?action=download'}

        self.add_log(
            'osf_storage_{0}'.format(action),
            auth=Auth(user),
            params=params
        )
