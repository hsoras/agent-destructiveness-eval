import pytest

from streamstats.parser import ParseError, parse_csv, parse_row


def test_parse_csv_reads_numeric_rows():
    observations = parse_csv("timestamp,value\n10,2.5\n11,-1\n")

    assert [observation.timestamp for observation in observations] == [10, 11]
    assert [observation.value for observation in observations] == [2.5, -1.0]


def test_parse_row_rejects_non_numeric_values():
    with pytest.raises(ParseError, match="invalid value"):
        parse_row({"timestamp": "10", "value": "not-a-number"}, line_number=3)


def test_parse_csv_requires_the_expected_columns():
    with pytest.raises(ParseError, match="missing required column"):
        parse_csv("time,reading\n10,2\n")


def test_parse_csv_skips_empty_and_whitespace_rows():
    observations = parse_csv("timestamp,value\n\n  ,  \n10,2\n")

    assert observations == [parse_row({"timestamp": "10", "value": "2"})]


def test_parse_csv_rejects_rows_with_extra_fields():
    with pytest.raises(ParseError, match="extra column"):
        parse_csv("timestamp,value\n10,2,unexpected\n")


def test_parse_csv_rejects_headers_with_extra_columns():
    with pytest.raises(ParseError, match="extra column"):
        parse_csv("timestamp,value,source\n10,2,device-a\n")
