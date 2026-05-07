"""Form schema for the Jobs tab's Add-job form.

This is the canonical declaration agents use to drive the form via
``dashboard_interact``. The Jobs help chat-walkthrough is the first
consumer; future "Help me create a job" surfaces (e.g. a Telegram
bot that talks to the dashboard) would target the same schema.

The fields here mirror the parameters of
``work_buddy.sidecar.scheduler.jobs.create_user_job_file`` — the form
is the user-facing wrapper, and the schema is the agent-facing wrapper
of the same underlying create flow.
"""

from __future__ import annotations

from work_buddy.dashboard.forms import Field, FormSchema, register_schema


JOBS_FORM_SCHEMA = FormSchema(
    form_id="jobs-add-job",
    description=(
        "Personal scheduled cron job. Schedule + a payload (a "
        "capability call, a workflow run, or a freeform prompt) "
        "fires when the cron matches."
    ),
    submit_label="Create job",
    fields=(
        Field(
            name="name",
            type="str",
            ui_id="job-form-name",
            required=True,
            description="Filename stem for the job. Becomes the job's identifier.",
            regex=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$",
        ),
        Field(
            name="schedule",
            type="cron",
            ui_id="job-form-schedule",
            required=True,
            description=(
                "5-field cron expression (MIN HOUR DOM MON DOW), evaluated "
                "in the configured timezone. Convert from natural language "
                "rather than asking the user to type cron syntax."
            ),
        ),
        Field(
            name="job_type",
            type="enum",
            ui_id="job-form-type",
            required=True,
            description=(
                "What kind of work fires when the cron matches: "
                "``capability`` (single registered capability), "
                "``workflow`` (multi-step workflow), or "
                "``prompt`` (freeform task — agent runs the prompt body)."
            ),
            enum_values=("capability", "workflow", "prompt"),
        ),
        Field(
            name="capability",
            type="str",
            ui_id="job-form-invoke-name",
            description=(
                "Registered capability name. Set only when "
                "job_type=capability. Use ``wb_search`` to confirm the "
                "capability exists before setting."
            ),
        ),
        Field(
            name="workflow",
            type="str",
            ui_id="job-form-invoke-name",
            description=(
                "Registered workflow name. Set only when job_type=workflow. "
                "Use ``wb_search`` to confirm the workflow exists."
            ),
        ),
        Field(
            name="prompt",
            type="str",
            ui_id="job-form-prompt",
            description=(
                "Prompt body the agent runs. Set only when job_type=prompt. "
                "Paste the user's natural-language description verbatim."
            ),
        ),
        Field(
            name="params",
            type="dict",
            ui_id="job-form-params",
            description=(
                "JSON parameters dict. Set only when job_type is capability "
                "or workflow and the chosen target declares parameters."
            ),
        ),
    ),
)

register_schema(JOBS_FORM_SCHEMA)
