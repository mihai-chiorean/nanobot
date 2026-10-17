"""Generate synthetic fixtures only. Requires pypdf and qpdf; no private inputs."""
import subprocess
import tempfile
from pathlib import Path

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject, NumberObject

for legacy in (False, True):
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})
    })
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 50 700 Td (RC4 R4 fixture document text) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    if legacy:
        # Not observed on the owner's file: isolate pdfcpu's validation gate.
        writer._info.get_object()[NameObject("/Trapped")] = NumberObject(7)
    name = "rc4-r4-legacy-info.pdf" if legacy else "rc4-r4.pdf"
    with tempfile.TemporaryDirectory() as directory:
        plain = Path(directory) / "plain.pdf"
        writer.write(plain)
        subprocess.run([
            "qpdf", "--allow-weak-crypto", str(plain), "--object-streams=generate",
            "--encrypt", "fixture-password", "owner-password", "128",
            "--use-aes=n", "--force-V4", "--", str(Path(__file__).with_name(name)),
        ], check=True)
