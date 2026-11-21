from io import StringIO
from unittest.mock import patch

from dotenv.main import DotEnv


def test_empty_parse_result_is_still_cached():
    obj = DotEnv(None, stream=StringIO(""))
    with patch.object(obj, "parse", wraps=obj.parse) as parse:
        assert obj.dict() == {}
        assert obj.dict() == {}
    assert parse.call_count == 1
