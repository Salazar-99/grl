from vms.images import NODE_BASES_DIR, NODE_TASKS_DIR, image_paths
from vms.tasks import task_record


def test_task_record_emits_node_relative_image_paths() -> None:
    row = {
        "instance_id": "sqlfluff__sqlfluff-1625",
        "repo": "sqlfluff/sqlfluff",
        "version": "0.6",
        "problem_statement": "Fix the bug",
        "base_commit": "abc123",
    }
    record = task_record(row, split="dev")
    assert record["base_image"] == "images/bases/sqlfluff__sqlfluff-0.6.squashfs"
    assert record["task_image"] == "images/tasks/sqlfluff__sqlfluff-1625.squashfs"
    assert record["task_id"] == "sqlfluff__sqlfluff-1625"
    assert record["split"] == "dev"


def test_image_paths_helper_matches_node_layout() -> None:
    row = {
        "instance_id": "django__django-11001",
        "repo": "django/django",
        "version": "3.0",
    }
    paths = image_paths(row, bases_dir=NODE_BASES_DIR, tasks_dir=NODE_TASKS_DIR)
    assert paths == {
        "base_image": "images/bases/django__django-3.0.squashfs",
        "task_image": "images/tasks/django__django-11001.squashfs",
    }
