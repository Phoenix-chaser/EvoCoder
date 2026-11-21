from dotenv import get_key, set_key


def test_trailing_backslash_does_not_consume_next_binding(tmp_path):
    env = tmp_path / "configuration.env"
    env.write_text("")
    set_key(env, "PATH_VALUE", "back\\")
    set_key(env, "NEXT_VALUE", "sentinel")
    assert get_key(env, "PATH_VALUE") == "back\\"
    assert get_key(env, "NEXT_VALUE") == "sentinel"
