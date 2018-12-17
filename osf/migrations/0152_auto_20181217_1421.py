from __future__ import unicode_literals

import logging

from django.db import migrations
from osf.utils.migrations import ensure_schemas


logger = logging.getLogger(__file__)


class Migration(migrations.Migration):

    dependencies = [
        ('osf', '0151_merge_20181203_1555'),
    ]

    operations = [
        migrations.RunPython(ensure_schemas, ensure_schemas),
    ]
