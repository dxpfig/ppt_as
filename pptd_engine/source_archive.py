"""Export-time source snapshot. Recovery never claims to merge later Office edits."""
from pathlib import Path, PurePosixPath
from zipfile import ZipFile, ZIP_DEFLATED
import base64
import hashlib
import io
import json
import stat
import tempfile
from lxml import etree

NAMESPACE='urn:ppt-as:pptd-source:v1'
REL='http://schemas.openxmlformats.org/package/2006/relationships'
OFFICE='http://schemas.openxmlformats.org/officeDocument/2006/relationships/customXml'
CT='http://schemas.openxmlformats.org/package/2006/content-types'


def embed_source(pptx_path, source):
    from .pptx import load_yaml, local_resource
    document=load_yaml(source)
    paths={source.resolve()}
    def resources(value):
        if isinstance(value,dict):
            for key,item in value.items():
                if key=='src' and isinstance(item,str) and '://' not in item:paths.add(local_resource(source.parent,item))
                else:resources(item)
        elif isinstance(value,list):
            for item in value:resources(item)
    resources(document)
    for page in document['pages']:
        path=local_resource(source.parent,page);paths.add(path);resources(load_yaml(path))
    stream=io.BytesIO()
    with ZipFile(stream,'w',ZIP_DEFLATED) as z:
        for path in sorted(paths):z.writestr(str(path.relative_to(source.parent)),path.read_bytes())
    payload=stream.getvalue()
    root=etree.Element('{'+NAMESPACE+'}source',entry=source.name,sha256=hashlib.sha256(payload).hexdigest(),meaning='export-time snapshot; later Office edits are not represented')
    root.text=base64.b64encode(payload).decode()
    with ZipFile(pptx_path) as z:parts={n:z.read(n) for n in z.namelist()}
    name='customXml/pptdSource.xml'
    parts[name]=etree.tostring(root,encoding='UTF-8',xml_declaration=True)
    rels=etree.fromstring(parts['ppt/_rels/presentation.xml.rels'])
    etree.SubElement(rels,'{'+REL+'}Relationship',Id='rIdPptdSource',Type=OFFICE,Target='../'+name)
    parts['ppt/_rels/presentation.xml.rels']=etree.tostring(rels,encoding='UTF-8',xml_declaration=True)
    ct=etree.fromstring(parts['[Content_Types].xml'])
    etree.SubElement(ct,'{'+CT+'}Override',PartName='/'+name,ContentType='application/xml')
    parts['[Content_Types].xml']=etree.tostring(ct,encoding='UTF-8',xml_declaration=True)
    with ZipFile(pptx_path,'w',ZIP_DEFLATED) as z:
        for name,data in parts.items():z.writestr(name,data)
    return {'part':'customXml/pptdSource.xml','files':len(paths),'sha256':root.get('sha256'),'meaning':root.get('meaning')}


def recover_source(pptx_path, output):
    """Validate the complete snapshot, then publish one complete recovered tree.

    Failure leaves an existing empty destination untouched; no partially
    extracted document is exposed. The entry must be an actual archive member.
    """
    output = Path(output).resolve()
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError('Recovery requires an empty destination')
    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    with ZipFile(pptx_path) as package:
        root = etree.fromstring(package.read('customXml/pptdSource.xml'), parser)
    if root.tag != '{' + NAMESPACE + '}source' or not isinstance(root.text, str):
        raise ValueError('Invalid PPTD source snapshot')
    data = base64.b64decode(root.text, validate=True)
    if hashlib.sha256(data).hexdigest() != root.get('sha256'):
        raise ValueError('Source snapshot hash mismatch')

    def relative_path(name):
        if not isinstance(name, str) or not name or '\\' in name or ':' in name or '\x00' in name:
            raise ValueError('Invalid snapshot path')
        relative = PurePosixPath(name)
        if relative.is_absolute() or '..' in relative.parts or str(relative) != name.rstrip('/') or str(relative) == '.':
            raise ValueError('Invalid snapshot path')
        return relative

    entry = relative_path(root.get('entry'))
    if entry.suffix != '.pptd':
        raise ValueError('Snapshot entry must name a .pptd document')
    with ZipFile(io.BytesIO(data)) as archive:
        members = {}
        for member in archive.infolist():
            relative = relative_path(member.filename)
            if relative in members or stat.S_ISLNK(member.external_attr >> 16):
                raise ValueError('Duplicate or linked snapshot member')
            members[relative] = member
        if entry not in members or members[entry].is_dir():
            raise ValueError('Snapshot entry is missing from the archive')
        # A file cannot also be the parent directory of another archive member.
        for relative in members:
            for parent in relative.parents:
                if parent in members and not members[parent].is_dir():
                    raise ValueError('Snapshot file/directory path conflict')
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.pptd-recovery-', dir=output.parent) as temporary:
            staged = Path(temporary) / 'document'
            staged.mkdir()
            for relative, member in members.items():
                path = staged.joinpath(*relative.parts)
                if member.is_dir():
                    path.mkdir(parents=True, exist_ok=True)
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(archive.read(member))  # CRC is verified before publication.
            staged.replace(output)
    return output.joinpath(*entry.parts)
