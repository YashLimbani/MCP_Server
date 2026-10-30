from textclean import MAX_QUERY_CHARS, clean_error_message


def test_python_traceback_picks_the_final_error_line():
    tb = (
        "Traceback (most recent call last):\n"
        '  File "/home/me/app.py", line 12, in <module>\n'
        "    x.foo()\n"
        "AttributeError: 'NoneType' object has no attribute 'foo'\n"
    )
    assert clean_error_message(tb) == "AttributeError: 'NoneType' object has no attribute 'foo'"


def test_file_paths_inside_the_message_are_removed():
    msg = "FileNotFoundError: [Errno 2] No such file or directory: '/home/me/data/in.csv'"
    assert clean_error_message(msg) == "FileNotFoundError: [Errno 2] No such file or directory"


def test_windows_paths_are_removed():
    msg = r"ImportError: DLL load failed while importing foo: C:\Users\me\venv\lib\foo.pyd"
    assert clean_error_message(msg) == "ImportError: DLL load failed while importing foo"


def test_hex_addresses_and_line_refs_are_removed():
    assert clean_error_message("ValueError: bad pointer 0x7ffd1234 in buffer") == (
        "ValueError: bad pointer in buffer"
    )


def test_compiler_style_location_prefix_is_removed():
    msg = "main.c:5:10: error: expected ';' before 'return'"
    assert clean_error_message(msg) == "error: expected ';' before 'return'"


def test_java_trace_uses_the_root_cause_line():
    trace = (
        'Exception in thread "main" java.lang.RuntimeException: wrapper\n'
        "\tat com.example.Main.run(Main.java:10)\n"
        "Caused by: java.io.IOException: disk full\n"
        "\t... 3 more\n"
    )
    assert clean_error_message(trace) == "java.io.IOException: disk full"


def test_javascript_error_ignores_stack_frames():
    msg = "TypeError: Cannot read properties of undefined (reading 'x')\n    at foo (/app/index.js:10:5)"
    assert clean_error_message(msg) == "TypeError: Cannot read properties of undefined (reading 'x')"


def test_without_an_error_keyword_the_first_line_is_used():
    assert clean_error_message("something odd happened\nmore detail") == "something odd happened"


def test_quoted_message_is_unwrapped():
    assert clean_error_message('"Connection reset by peer"') == "Connection reset by peer"


def test_long_messages_are_truncated_on_a_word_boundary():
    out = clean_error_message("Error: " + "word " * 100)
    assert len(out) <= MAX_QUERY_CHARS
    assert not out.endswith("wor")


def test_empty_input():
    assert clean_error_message("") == ""
    assert clean_error_message("   \n  ") == ""
