from domains.executors.claude_code import with_turn_cap


def test_cap_appended():
    assert with_turn_cap(["claude", "-p", "x"], 200) == ["claude", "-p", "x", "--max-turns", "200"]


def test_none_and_zero_add_nothing():
    assert with_turn_cap(["claude", "-p", "x"], None) == ["claude", "-p", "x"]
    assert with_turn_cap(["claude", "-p", "x"], 0) == ["claude", "-p", "x"]


def test_operator_set_flag_respected():
    argv = ["claude", "-p", "x", "--max-turns", "50"]
    assert with_turn_cap(argv, 200) == argv
