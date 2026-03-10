from pyphony.cli import parse_args


class TestParseArgs:
    def test_default_workflow_file(self):
        args = parse_args([])
        assert args.workflow_file == "WORKFLOW.md"

    def test_custom_workflow_file(self):
        args = parse_args(["my_workflow.md"])
        assert args.workflow_file == "my_workflow.md"

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
        assert args.workflow_file == "custom.md"

    def test_run_subcommand_explicit(self):
        args = parse_args(["run", "my_workflow.md"])
        assert args.command == "run"
        assert args.workflow_file == "my_workflow.md"
