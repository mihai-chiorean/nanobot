Synthetic one-page PDFs. No bytes or passwords from the owner's file.

Run python generate.py with pypdf and qpdf installed. Fixture password:
fixture-password. Both fixtures use PDF 1.5, /Standard V=4 R=4, 128-bit
RC4, /StdCF with /CFM /V2 and /Length 16, encrypted metadata, object streams,
and all permissions allowed (P=-4). pypdf produces the page; qpdf encrypts it.

rc4-r4.pdf is the ordinary control: deployed pdfcpu already handles it.
rc4-r4-legacy-info.pdf adds invalid optional /Info /Trapped=7. pdfcpu 0.16.2
rejects it in strict AND relaxed validation; qpdf decrypts it and Poppler
extracts the expected fixture text. This isolates a reproducible validation
failure, not evidence that the owner's document has this metadata defect.

The only confirmed owner-file metadata is R=4, P=-4 and RC4 for streams,
strings and files (qpdf --show-encryption, no password). The other generator
settings are test choices, not established properties of the private PDF.
