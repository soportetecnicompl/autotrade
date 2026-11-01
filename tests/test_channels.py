import configparser
import unittest
from types import SimpleNamespace as NS

from autotrade.channels import (ChannelCfg, build_folders, channel_ref, folder_members, folder_title,
                                next_num, parse_channels, render_channels)

DIALOGS = [
    {"id": -1001, "kind": "canal", "muted": False, "archived": False, "unread": 3},
    {"id": -1002, "kind": "canal", "muted": True, "archived": False, "unread": 0},
    {"id": -1003, "kind": "grupo", "muted": False, "archived": True, "unread": 1},
    {"id": -1004, "kind": "canal", "muted": False, "archived": False, "unread": 0},
]
ids = lambda ds: [d["id"] for d in ds]


class FolderLogicTests(unittest.TestCase):
    def test_explicit_include(self):
        self.assertEqual(ids(folder_members({"include": {-1001, -1004}}, DIALOGS)), [-1001, -1004])

    def test_category_flags(self):
        self.assertEqual(ids(folder_members({"broadcasts": True}, DIALOGS)), [-1001, -1002, -1004])
        self.assertEqual(ids(folder_members({"groups": True}, DIALOGS)), [-1003])
        self.assertEqual(ids(folder_members({"broadcasts": True, "groups": True}, DIALOGS)), [-1001, -1002, -1003, -1004])

    def test_exclusions_only_filter_category_matches_not_explicit_ones(self):
        spec = {"broadcasts": True, "include": {-1002}, "exclude_muted": True, "exclude_read": True}
        # -1002 está silenciado pero incluido a mano -> aparece; -1004 está leído -> sale; -1001 tiene no leídos
        self.assertEqual(ids(folder_members(spec, DIALOGS)), [-1001, -1002])

    def test_excluded_peers_always_win_and_archived_filter(self):
        self.assertEqual(ids(folder_members({"broadcasts": True, "exclude": {-1001}}, DIALOGS)), [-1002, -1004])
        self.assertEqual(ids(folder_members({"groups": True, "exclude_archived": True}, DIALOGS)), [])
        self.assertEqual(ids(folder_members({"groups": True, "include": {-1003}, "exclude_archived": True}, DIALOGS)), [-1003])

    def test_empty_spec_matches_nothing(self):
        self.assertEqual(folder_members({}, DIALOGS), [])


class BuildFoldersTests(unittest.TestCase):
    """Imita los objetos de Telethon (se identifican por el nombre de su clase)."""

    def make(self, cls_name, **kw):
        return type(cls_name, (), kw)()

    def test_builds_folders_skips_default_and_handles_title_objects(self):
        peer = lambda i: NS(pid=i)
        f_old = self.make("DialogFilter", id=2, title="Señales", include_peers=[peer(-1001), peer(-1004)],
                          pinned_peers=[], exclude_peers=[], broadcasts=False)
        f_new = self.make("DialogFilter", id=3, title=NS(text="VIP Gold"), include_peers=[], pinned_peers=[peer(-1003)],
                          exclude_peers=[], broadcasts=True, exclude_muted=True)
        f_chatlist = self.make("DialogFilterChatlist", id=4, title="Compartida", include_peers=[peer(-1002)], pinned_peers=[])
        default = self.make("DialogFilterDefault")
        folders = build_folders([default, f_old, f_new, f_chatlist], DIALOGS, lambda p: p.pid)
        self.assertEqual([(f["id"], f["title"]) for f in folders], [(2, "Señales"), (3, "VIP Gold"), (4, "Compartida")])
        self.assertEqual([f["members"] for f in folders],
                         [[-1001, -1004], [-1001, -1003, -1004], [-1002]])   # -1002 silenciado: fuera por categoría

    def test_bad_peer_does_not_break_the_folder(self):
        def peer_id(p):
            if p == "raro":
                raise ValueError("peer no soportado")
            return p
        f = self.make("DialogFilter", id=5, title="X", include_peers=["raro", -1001], pinned_peers=[], exclude_peers=[])
        self.assertEqual(build_folders([f], DIALOGS, peer_id)[0]["members"], [-1001])

    def test_helpers(self):
        self.assertEqual(folder_title(NS(text=" Hola ")), "Hola")
        self.assertEqual(folder_title(None), "")
        self.assertEqual(build_folders(None, DIALOGS, lambda p: p), [])


class ChannelConfigTests(unittest.TestCase):
    def test_render_parse_roundtrip_and_stable_numbers(self):
        chans = [ChannelCfg(2, "-1002", "B"), ChannelCfg(5, "@c", "C", False, 0.03, 2)]
        cp = configparser.ConfigParser(interpolation=None)
        cp.read_string(render_channels(chans))
        self.assertEqual(parse_channels(cp), chans)
        self.assertEqual(next_num(chans), 6)                       # nunca reutiliza números intermedios
        self.assertEqual(next_num([]), 1)
        self.assertEqual(render_channels([]), "")

    def test_ignores_incomplete_sections_and_bad_numbers(self):
        cp = configparser.ConfigParser(interpolation=None)
        cp.read_string("[channel.1]\nref =\n[channel.2]\nref = @x\nlot_size = abc\nmax_tps = 9x\n")
        out = parse_channels(cp)
        self.assertEqual([(c.num, c.ref, c.lot_size, c.max_tps) for c in out], [(2, "@x", 0.0, 0)])

    def test_title_with_newline_cannot_break_the_file(self):
        txt = render_channels([ChannelCfg(1, "-1", "Línea1\nLínea2")])
        cp = configparser.ConfigParser(interpolation=None)
        cp.read_string(txt)
        self.assertEqual(parse_channels(cp)[0].title, "Línea1 Línea2")

    def test_channel_ref(self):
        self.assertEqual(channel_ref({"id": -1005, "username": "goldpro"}), "@goldpro")
        self.assertEqual(channel_ref({"id": -1005, "username": ""}), "-1005")


if __name__ == "__main__":
    unittest.main()
