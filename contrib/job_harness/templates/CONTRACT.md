# CONTRACT  {{JOB}}
# Required: goal output scope never done_when budget. The wrapper refuses to start without them.
# Continuation lines are indented. done_when line 1 is the command the wrapper runs (must be able to exit non-zero).

goal        <one sentence: what changes in the world>
output      <files / PR / report the job must leave behind>
scope       <paths the job may write; everything else is read-only>
never       {{HOST_NEVER}}
            <job-specific never items; you may add, never remove host defaults>
done_when   bash {{JOB_DIR}}/done_when.sh
            <prose: what done_when.sh checks>
budget      max_wall_s=3600 max_attempts=2 max_same_error=2
# optional:
# prompt    prompt.md
# judge     cmd: <command that exits 0 only if the result matches the contract>
# runner    <script to run instead of the default agent chain; gets $JOB_PROMPT_FILE>
