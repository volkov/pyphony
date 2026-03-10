from pyphony.cli import parse_args


class TestParseArgs:
    def test_default_workflow_files(self):
        args = parse_args([])
        assert args.workflow_files == ["WORKFLOW.md"]
        assert args.workflow_file == "WORKFLOW.md"

    def test_single_workflow_file(self):
        args = parse_args(["my_workflow.md"])
        assert args.workflow_files == ["my_workflow.md"]
        assert args.workflow_file == "my_workflow.md"

    def test_multiple_workflow_files(self):
        args = parse_args(["run", "wf1.md", "wf2.md", "wf3.md"])
        assert args.workflow_files == ["wf1.md", "wf2.md", "wf3.md"]
        assert args.workflow_file == "wf1.md"

    def test_multiple_workflow_files_without_run(self):
        args = parse_args(["wf1.md", "wf2.md"])
        assert args.workflow_files == ["wf1.md", "wf2.md"]

    def test_port_flag(self):
        args = parse_args(["--port", "8080"])
        assert args.port == 8080

    def test_port_default(self):
        args = parse_args([])
        assert args.port is None

    def test_log_level(self):
        args = parse_args(["--log-level", "DEBUG"])
        assert args.log_level == "DEBUG"

    def test_log_level_default(self):
        args = parse_args([])
        assert args.log_level == "INFO"

    def test_all_options(self):
        args = parse_args(["--port", "3000", "--log-level", "WARNING", "custom.md"])
        assert args.workflow_files == ["custom.md"]
        assert args.workflow_file == "custom.md"
        assert args.port == 3000
        assert args.log_level == "WARNING"

    def test_create_issue_subcommand(self):
        args = parse_args(["create-issue", "--title", "Test task"])
        assert args.command == "create-issue"
        assert args.title == "Test task"
        assert args.description is None
        assert args.workflow_file == "WORKFLOW.md"

    def test_create_issue_with_description(self):
        args = parse_args([
            "create-issue", "--title", "Test", "--description", "Details here",
            "custom.md",
        ])
        assert args.command == "create-issue"
        assert args.title == "Test"
        assert args.description == "Details here"
        assert args.workflow_files == ["custom.md"]
        assert args.workflow_file == "custom.md"

    def test_run_subcommand_explicit(self):
        args = parse_args(["run", "my_workflow.md"])
        assert args.command == "run"
        assert args.workflow_files == ["my_workflow.md"]
        assert args.workflow_file == "my_workflow.md"

    def test_run_subcommand_multiple_files(self):
        args = parse_args(["run", "wf1.md", "wf2.md", "--exit-on-merge"])
        assert args.command == "run"
        assert args.workflow_files == ["wf1.md", "wf2.md"]
        assert args.exit_on_merge is True

    def test_get_issue_subcommand(self):
        args = parse_args(["get-issue", "--identifier", "SER-27"])
        assert args.command == "get-issue"
        assert args.identifier == "SER-27"
        assert args.workflow_file == "WORKFLOW.md"

    def test_update_issue_subcommand(self):
        args = parse_args([
            "update-issue", "--identifier", "SER-27",
            "--title", "New title", "--state", "Done",
        ])
        assert args.command == "update-issue"
        assert args.identifier == "SER-27"
        assert args.title == "New title"
        assert args.state == "Done"
        assert args.description is None
        assert args.workflow_file == "WORKFLOW.md"

    def test_update_issue_with_description(self):
        args = parse_args([
            "update-issue", "--identifier", "SER-27",
            "--description", "Updated description",
            "custom.md",
        ])
        assert args.command == "update-issue"
        assert args.identifier == "SER-27"
        assert args.description == "Updated description"
        assert args.title is None
        assert args.state is None
        assert args.workflow_files == ["custom.md"]
        assert args.workflow_file == "custom.md"
