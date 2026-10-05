from picklist.domain.pick_path import pick_line_sort_key


def test_racks_and_shelves_descend_numerically():
    locations = ['R01S03', 'R09S01', 'R01S05', 'R10S02', 'R09S05', 'A02', 'A10']
    lines = [{'location': location} for location in locations]
    assert [line['location'] for line in sorted(lines, key=pick_line_sort_key)] == [
        'R10S02', 'R09S05', 'R09S01', 'R01S05', 'R01S03', 'A02', 'A10']
