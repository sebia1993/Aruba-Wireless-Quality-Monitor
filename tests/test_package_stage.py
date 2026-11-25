import os,tempfile,unittest,zipfile,stat
from pathlib import Path
from unittest.mock import patch
from awqm.package_stage import stage,digest

class StageTests(unittest.TestCase):
    def package(self,root,extra=(),omit=None):
        p=root/"package.zip"
        with zipfile.ZipFile(p,"w") as z:
            for name in ("AWQM.exe","AWQM_Service.exe","AWQM_Manager.exe","VERSION.txt","README.md","BUILD-DEPENDENCIES.txt","_internal/python.dll","docs/guide.md"):
                if name!=omit:z.writestr("AWQM/"+name,"0.20.0-dev\n" if name=="VERSION.txt" else "fixture")
            for name,data in extra:
                if isinstance(name,str):
                    # Preserve raw spelling on Windows during fixture construction.
                    i=zipfile.ZipInfo(name);i.filename=name;i.orig_filename=name
                    z.writestr(i,data)
                else:z.writestr(name,data)
        return p
    def test_verified_staging_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);z=self.package(p)
            out=p/"new"
            self.assertEqual(stage(z,digest(z),out)["status"],"verified_staged_not_installed")
            self.assertTrue((out/"AWQM.exe").is_file())
            with self.assertRaises(ValueError):stage(z,digest(z),out)
    def test_bad_packages(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)
            for member in ("AWQM/../bad","AWQM/CON.txt","AWQM/x. ","AWQM/x:stream","AWQM/awqm.exe","AWQM/dir\\bad","AWQM/README.md/child","AWQM/CON .txt","AWQM/CONOUT$","AWQM/Stage-Receipt.json","AWQM/Docs/extra.md"):
                with self.subTest(member=member):
                    z=self.package(p,[(member,"bad")])
                    with self.assertRaises(ValueError):stage(z,digest(z),p/"new")
                    self.assertFalse((p/"new").exists())
            z=self.package(p,omit="AWQM_Service.exe")
            with self.assertRaises(ValueError):stage(z,digest(z),p/"new")
            z=self.package(p)
            with self.assertRaises(ValueError):stage(z,"0"*64,p/"new")
            i=zipfile.ZipInfo("AWQM/link");i.create_system=3;i.external_attr=(stat.S_IFLNK|0o777)<<16
            z=self.package(p,[(i,"outside")])
            with self.assertRaises(ValueError):stage(z,digest(z),p/"new")

    @unittest.skipUnless(os.name == "nt", "Windows DACL inheritance")
    def test_published_tree_inherits_destination_acl_at_each_depth(self):
        import win32security as security
        def acl(path):
            desc = security.GetNamedSecurityInfo(str(path), security.SE_FILE_OBJECT,
                                                 security.DACL_SECURITY_INFORMATION)
            dacl = desc.GetSecurityDescriptorDacl()
            return [(ace[0], ace[1], security.ConvertSidToStringSid(ace[2]))
                    for ace in (dacl.GetAce(i) for i in range(dacl.GetAceCount()))]

        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            dest = parent / "destination"
            dest.mkdir()
            desc = security.GetNamedSecurityInfo(str(dest), security.SE_FILE_OBJECT,
                                                 security.DACL_SECURITY_INFORMATION)
            dacl = desc.GetSecurityDescriptorDacl()
            # The synthetic extra read grant distinguishes the destination
            # from Python's private TemporaryDirectory DACL.
            reader = security.CreateWellKnownSid(security.WinLocalServiceSid, None)
            dacl.AddAccessAllowedAceEx(security.ACL_REVISION, 3, 0x120089, reader)
            security.SetNamedSecurityInfo(str(dest), security.SE_FILE_OBJECT,
                                          security.DACL_SECURITY_INFORMATION, None, None, dacl, None)
            normal = dest / "ordinary-directory"
            (normal / "_internal").mkdir(parents=True)
            (normal / "_internal/python.dll").touch()
            (normal / "STAGE-RECEIPT.json").touch()
            archive = self.package(parent)
            delivered = dest / "staged"
            stage(archive, digest(archive), delivered)
            for rel in (".", "_internal", "_internal/python.dll", "STAGE-RECEIPT.json"):
                with self.subTest(rel=rel):
                    self.assertEqual(acl(delivered / rel), acl(normal / rel))

    @unittest.skipUnless(os.name == "nt", "Windows DACL inheritance")
    def test_failed_acl_publication_does_not_leave_a_candidate(self):
        import win32security
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            archive = self.package(parent)
            with patch.object(win32security, "SetSecurityInfo", side_effect=OSError("injected ACL failure")):
                with self.assertRaisesRegex(OSError, "injected ACL failure"):
                    stage(archive, digest(archive), parent / "candidate")
            self.assertEqual(list(parent.iterdir()), [archive])

    @unittest.skipUnless(os.name == "nt", "Windows no-replace publication")
    def test_competing_publication_keeps_existing_destination_intact(self):
        import pywintypes
        import win32security
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            archive = self.package(parent)
            destination = parent / "candidate"
            apply_acl = win32security.SetSecurityInfo
            def publish_other_first(*args):
                apply_acl(*args)
                destination.mkdir()
                (destination / "other").write_bytes(b"preserve")
            with patch.object(win32security, "SetSecurityInfo", side_effect=publish_other_first):
                with self.assertRaises(pywintypes.error):
                    stage(archive, digest(archive), destination)
            self.assertEqual({p.name for p in parent.iterdir()}, {"package.zip", "candidate"})
            self.assertEqual([p.name for p in destination.iterdir()], ["other"])
            self.assertEqual((destination / "other").read_bytes(), b"preserve")
