from htmltext import html_to_markdown, truncate_text


def test_code_block_keeps_indentation_and_blank_lines():
    html = "<pre><code>def f():\n    return 1\n\n\ndef g():\n    pass\n</code></pre>"
    md = html_to_markdown(html)
    assert md == "```\ndef f():\n    return 1\n\n\ndef g():\n    pass\n```"


def test_code_block_language_hint():
    md = html_to_markdown('<pre class="lang-py prettyprint-override"><code>x = 1\n</code></pre>')
    assert md.startswith("```py\n")


def test_entities_decoded_in_code_and_text():
    md = html_to_markdown("<p>a =&gt; b</p><pre><code>print(&quot;hi&quot;)\n</code></pre>")
    assert "a => b" in md
    assert 'print("hi")' in md


def test_inline_code_links_emphasis():
    md = html_to_markdown(
        '<p>Use <code>asyncio</code> and <a href="https://x.dev/doc">the docs</a>, <em>maybe</em>.</p>'
    )
    assert md == "Use `asyncio` and [the docs](https://x.dev/doc), *maybe*."


def test_link_with_same_text_as_url_is_not_duplicated():
    md = html_to_markdown('<p><a href="https://x.dev">https://x.dev</a></p>')
    assert md == "https://x.dev"


def test_headings_lists_and_hr():
    md = html_to_markdown("<h2>Title</h2><ul><li>one</li><li>two</li></ul><hr /><p>end</p>")
    assert md == "## Title\n\n- one\n- two\n\n---\n\nend"


def test_ordered_and_nested_lists():
    md = html_to_markdown("<ol><li>a</li><li>b<ul><li>c</li></ul></li></ol>")
    assert md == "1. a\n2. b\n  - c"


def test_blockquote_is_prefixed():
    md = html_to_markdown("<blockquote><p>quoted</p><ul><li>item</li></ul></blockquote>")
    assert md == "> quoted\n>\n> - item"


def test_no_html_tags_left():
    md = html_to_markdown("<p>x <sup>(<a href='u'>y</a>)</sup> <img src='i.png' alt='pic'></p>")
    assert "<" not in md
    assert "[image: pic]" in md


def test_malformed_unclosed_pre_is_closed():
    md = html_to_markdown("<pre><code>x = 1")
    assert md.count("```") == 2


def test_truncate_short_text_unchanged():
    assert truncate_text("short", 100, "https://l") == "short"


def test_truncate_adds_marker_and_cuts_on_boundary():
    text = "word " * 100
    out = truncate_text(text, 50, "https://l")
    assert out.endswith("[truncated, see https://l]")
    assert len(out) < 50 + 40


def test_truncate_closes_open_code_fence():
    md = html_to_markdown("<pre><code>" + "x = 1\n" * 100 + "</code></pre>")
    out = truncate_text(md, 60, "https://l")
    assert out.count("```") % 2 == 0
    assert out.endswith("[truncated, see https://l]")
