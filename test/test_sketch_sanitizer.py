from backend.sketch_sanitizer import sanitize_sketch


def test_sketch_sanitizer_removes_active_and_remote_content() -> None:
    source = """
    <html><head><script>alert(1)</script><link rel="stylesheet" href="https://bad"></head>
    <body onload="steal()">
      <h1 style="color: blue">Architecture</h1>
      <img src="https://bad/pixel">
      <a href="javascript:steal()">click</a>
      <div style="background:url(https://bad)">Flow</div>
      <iframe src="https://bad"></iframe>
    </body></html>
    """

    result = sanitize_sketch(source)

    assert "Content-Security-Policy" in result
    assert "Architecture" in result
    assert "Flow" in result
    assert "color: blue" in result
    assert "script" not in result
    assert "onload" not in result
    assert "javascript:" not in result
    assert "https://bad" not in result
    assert "iframe" not in result
    assert "url(" not in result


def test_sketch_export_is_pinned_to_a_light_theme() -> None:
    result = sanitize_sketch("<p>x</p>")

    assert "color-scheme:light}" in result
    assert "light dark" not in result
    assert "background:#fff !important" in result
    assert ".mermaid svg{width:100%;max-width:none !important" in result
