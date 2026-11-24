import tempfile,unittest,zipfile,stat
from pathlib import Path
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
