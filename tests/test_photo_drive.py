"""photo_drive: связь «фото архива → файл на Google Диске».

Закреплено то, без чего связь врёт молча:
- имя файла — sha1 всей ссылки, с query; другая схема ослепит индекс без ошибок;
- индекс пишется, только если каждый файл на Диске совпал с локальным по
  размеру, md5 и sha256, и не теряет имён без --allow-loss;
- link называет фото, которого на Диске нет, и не повторяет ссылку VK.

Фикстуры выдуманы: ссылки на example.invalid, id и хеши из одной буквы.
"""
import hashlib
import io
import json

import pytest

import photo_drive

URL_A = "https://example.invalid/c/a1.jpg?size=604x453&quality=95"
URL_B = "https://example.invalid/c/b2.jpg"
URL_GONE = "https://example.invalid/c/zz.jpg"
ID_A = "1" + "a" * 32
ID_B = "1" + "b" * 32


def name_of(url: str) -> str:
    # Считается здесь, а не через dest_for: файлы на Диске уже названы так,
    # и если схема у загрузчика изменится, набор обязан покраснеть.
    return hashlib.sha1(url.encode()).hexdigest()[:16] + ".jpg"


def entry(url: str, drive_id: str, size: int, digit: str) -> dict:
    name = name_of(url)
    return {"Path": name, "Name": name, "Size": size, "MimeType": "image/jpeg",
            "ModTime": "2026-01-01T00:00:00Z", "IsDir": False, "ID": drive_id,
            "Hashes": {"md5": digit * 32, "sha1": digit * 40, "sha256": digit * 64}}


def listing() -> list[dict]:
    return [entry(URL_A, ID_A, 1000, "1"), entry(URL_B, ID_B, 2000, "2")]


LOCAL = {name_of(URL_A): (1000, "1" * 32, "1" * 64),
         name_of(URL_B): (2000, "2" * 32, "2" * 64)}


@pytest.fixture
def files(tmp_path):
    listing_path = tmp_path / "listing.json"
    listing_path.write_text(json.dumps(listing()), encoding="utf-8")
    tsv = tmp_path / "local.tsv"
    tsv.write_text("name\tsize\tmd5\tsha256\n"
                   + "".join(f"{n}\t{s}\t{m}\t{h}\n" for n, (s, m, h) in LOCAL.items()),
                   encoding="utf-8")
    return listing_path, tsv, tmp_path / "photo_drive.jsonl"


@pytest.fixture
def index_file(files, capsys):
    listing_path, _, out = files
    assert photo_drive.main(["index", str(listing_path), "--out", str(out)]) == 0
    capsys.readouterr()
    return out


def test_name_scheme_is_pinned():
    assert photo_drive.resolve(URL_A, {}) == ("0b4843f803fee2bc.jpg", None)
    assert photo_drive.resolve(URL_A.split("?")[0], {})[0] == "9c0e02fea804d81c.jpg"


def test_listing_becomes_index_rows():
    rows = photo_drive.parse_listing(listing())
    assert rows[name_of(URL_A)] == {"name": name_of(URL_A), "drive_id": ID_A, "bytes": 1000,
                                    "md5": "1" * 32, "sha256": "1" * 64}
    assert set(rows) == {name_of(URL_A), name_of(URL_B)}


@pytest.mark.parametrize("change, reason", [
    (lambda ls: ls.append({**ls[0], "IsDir": True, "Name": "sub", "Path": "sub"}),
     "каталог sub"),
    (lambda ls: ls[0].update(Path="sub/" + ls[0]["Name"]), "не в корне папки"),
    (lambda ls: ls.append(dict(ls[0])), "два файла"),
    (lambda ls: ls[0].pop("ID"), "нет id"),
    (lambda ls: ls[0]["Hashes"].pop("md5"), "нет md5"),
    (lambda ls: ls[0]["Hashes"].update(sha256=""), "нет sha256"),
    (lambda ls: ls[0].pop("Size"), "нет размера"),
    (lambda ls: ls[0].update(Size=-1), "нет размера"),
    (lambda ls: ls[0].pop("Name"), "запись без имени"),
    (lambda ls: ls.clear(), "листинг пуст"),
])
def test_listing_refusal_names_the_reason(change, reason):
    ls = listing()
    change(ls)
    with pytest.raises(photo_drive.Refused, match=reason):
        photo_drive.parse_listing(ls)


@pytest.mark.parametrize("change, finding", [
    (lambda loc: loc.update({name_of(URL_A): (1001, "1" * 32, "1" * 64)}),
     "размер локально 1001"),
    (lambda loc: loc.update({name_of(URL_A): (1000, "9" * 32, "1" * 64)}), "md5 локально"),
    (lambda loc: loc.update({name_of(URL_A): (1000, "1" * 32, "9" * 64)}), "sha256 локально"),
    (lambda loc: loc.update({name_of(URL_GONE): (1, "3" * 32, "3" * 64)}),
     "был локально, на Диске нет"),
    (lambda loc: loc.pop(name_of(URL_B)), "на Диске есть, локально не было"),
])
def test_compare_names_each_mismatch(change, finding):
    local = dict(LOCAL)
    change(local)
    problems = photo_drive.compare(photo_drive.parse_listing(listing()), local)
    assert len(problems) == 1
    assert finding in problems[0]


def test_compare_equal_is_no_finding():
    assert photo_drive.compare(photo_drive.parse_listing(listing()), dict(LOCAL)) == []


def test_index_writes_rows_proven_by_local_hashes(files, capsys):
    listing_path, tsv, out = files
    rc = photo_drive.main(["index", str(listing_path), "--local-hashes", str(tsv),
                           "--out", str(out)])
    assert rc == 0
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert {r["name"]: r["drive_id"] for r in rows} == {name_of(URL_A): ID_A,
                                                        name_of(URL_B): ID_B}
    assert "сверено с локальными хешами: 2" in capsys.readouterr().out
    assert [p.name for p in out.parent.iterdir() if p.suffix == ".tmp"] == []


def test_index_mismatch_leaves_old_index_untouched(index_file, files, capsys):
    listing_path, tsv, out = files
    before = out.read_bytes()
    bad = listing()
    bad[0]["Hashes"]["sha256"] = "9" * 64
    listing_path.write_text(json.dumps(bad), encoding="utf-8")
    rc = photo_drive.main(["index", str(listing_path), "--local-hashes", str(tsv),
                           "--out", str(out)])
    assert rc == 1
    assert out.read_bytes() == before
    err = capsys.readouterr().err
    assert "sha256 локально" in err and "индекс не записан" in err


def test_index_refuses_to_lose_names_unless_told(index_file, files, capsys):
    listing_path, _, out = files
    before = out.read_bytes()
    listing_path.write_text(json.dumps(listing()[:1]), encoding="utf-8")
    assert photo_drive.main(["index", str(listing_path), "--out", str(out)]) == 1
    assert out.read_bytes() == before
    assert f"{name_of(URL_B)}: был в индексе, в листинге нет" in capsys.readouterr().err
    assert photo_drive.main(["index", str(listing_path), "--out", str(out),
                             "--allow-loss"]) == 0
    assert len(out.read_text(encoding="utf-8").splitlines()) == 1


def test_index_may_grow(files):
    listing_path, _, out = files
    listing_path.write_text(json.dumps(listing()[:1]), encoding="utf-8")
    assert photo_drive.main(["index", str(listing_path), "--out", str(out)]) == 0
    listing_path.write_text(json.dumps(listing()), encoding="utf-8")
    assert photo_drive.main(["index", str(listing_path), "--out", str(out)]) == 0
    assert len(out.read_text(encoding="utf-8").splitlines()) == 2


def test_index_unreadable_input_is_rc2_and_writes_nothing(files, tmp_path):
    listing_path, tsv, out = files
    assert photo_drive.main(["index", str(tmp_path / "nope.json"), "--out", str(out)]) == 2
    tsv.write_text("name\tsize\n", encoding="utf-8")
    assert photo_drive.main(["index", str(listing_path), "--local-hashes", str(tsv),
                             "--out", str(out)]) == 2
    assert not out.exists()


def test_link_finds_the_file_and_does_not_repeat_the_vk_url(index_file, capsys):
    assert photo_drive.main(["link", URL_A, "--index", str(index_file)]) == 0
    assert capsys.readouterr().out == (
        f"{name_of(URL_A)}\thttps://drive.google.com/file/d/{ID_A}/view\n")


def test_link_names_what_is_not_on_drive(index_file, capsys):
    assert photo_drive.main(["link", URL_B, URL_GONE, "--index", str(index_file)]) == 1
    assert capsys.readouterr().out.splitlines() == [
        f"{name_of(URL_B)}\thttps://drive.google.com/file/d/{ID_B}/view",
        f"{name_of(URL_GONE)}\tнет на Диске"]


def test_link_reads_urls_from_stdin(index_file, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(f"{URL_A}\n\n{URL_B}\n"))
    assert photo_drive.main(["link", "-", "--index", str(index_file)]) == 0
    assert len(capsys.readouterr().out.splitlines()) == 2


@pytest.mark.parametrize("content", [None, "", '{"name": "x.jpg"}\n', "not json\n"])
def test_link_without_a_usable_index_is_rc2(tmp_path, content, capsys):
    idx = tmp_path / "photo_drive.jsonl"
    if content is not None:
        idx.write_text(content, encoding="utf-8")
    assert photo_drive.main(["link", URL_A, "--index", str(idx)]) == 2
    assert "не прочитано" in capsys.readouterr().err


def test_coverage_counts_photo_rows_only(index_file, tmp_path, capsys):
    manifest = tmp_path / "media_manifest.jsonl"
    rows = [{"kind": "photo", "url": URL_A}, {"kind": "photo", "url": URL_A},
            {"kind": "photo", "url": URL_GONE}, {"kind": "voice", "url": URL_B},
            {"kind": "photo", "url": ""}]
    manifest.write_text("".join(json.dumps(r) + "\n" for r in rows) + "\n", encoding="utf-8")
    c = photo_drive.coverage(manifest.read_text(encoding="utf-8").splitlines(),
                             photo_drive.load_index(index_file))
    assert (c["photo"], c["on_drive"], c["not_on_drive"]) == (3, 2, 1)
    assert (c["indexed"], c["unreferenced"]) == (2, 1)
    assert photo_drive.main(["coverage", "--manifest", str(manifest),
                             "--index", str(index_file)]) == 0
    assert "с файлом на Диске: 2" in capsys.readouterr().out
