from convene import demo
from convene.__main__ import main


def test_demo_runs_and_every_refusal_happens():
    lines = []
    demo.run(out=lines.append)
    text = "\n".join(lines)
    assert "!!" not in text                                  # nothing that should be refused got through
    assert text.count("REFUSED") == 8
    assert "20 simultaneous bookings: 5 confirmed, 15 waitlisted, 0 overbooked" in text
    assert "capacity 7, confirmed 7, waitlisted 1, checked in 3" in text


def test_command_line(capsys):
    assert main(["demo"]) == 0
    assert "Welcome, Kerry-Ann" in capsys.readouterr().out
