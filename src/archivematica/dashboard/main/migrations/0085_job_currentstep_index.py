from django.db import migrations
from django.db import models


class Migration(migrations.Migration):
    """Index Job.currentstep on its own.

    MCPServer runs ``SELECT ... FROM Jobs WHERE currentStep = 1`` when it starts. No index of Jobs begins with
    currentStep (``jobType, currentStep`` and ``SIPUUID, currentStep, ...`` carry it second), so MySQL scanned the whole
    index: 383,000 rows in the dev database, 18 minutes with the default InnoDB buffer pool, during which every new
    connection waited. With the index the query examines a single row.
    """

    dependencies = [
        ("main", "0084_alter_job_index_together"),
    ]

    operations = [
        migrations.AlterField(
            model_name="job",
            name="currentstep",
            field=models.IntegerField(
                choices=[
                    (0, "Unknown"),
                    (1, "Awaiting decision"),
                    (2, "Completed successfully"),
                    (3, "Executing command(s)"),
                    (4, "Failed"),
                ],
                db_column="currentStep",
                db_index=True,
                default=0,
            ),
        ),
    ]
