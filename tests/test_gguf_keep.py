from hailports_setup.gguf_keep import keep_paths


def test_keep_includes_projector_and_draft(tmp_path):
    main = tmp_path / "model.gguf"
    proj = tmp_path / "mmproj.gguf"
    draft = tmp_path / "draft.gguf"
    for path in (main, proj, draft):
        path.write_bytes(b"x")
    argv = ["llama-server", "-m", str(main), "--mmproj", str(proj), "-md", str(draft)]
    got = set(keep_paths(argv, f"--mmproj {proj} -md {draft}"))
    assert got == {str(main.resolve()), str(proj.resolve()), str(draft.resolve())}


def test_extra_args_keep_projector_when_server_is_down(tmp_path):
    proj = tmp_path / "mmproj.gguf"
    proj.write_bytes(b"x")
    got = keep_paths([], f"--cache-ram 512 --mmproj {proj}")
    assert got == [str(proj.resolve())]
