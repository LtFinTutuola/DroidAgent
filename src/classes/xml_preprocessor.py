import re
import copy
from typing import List, Dict
from lxml import etree
from src.shared.shared_constants import logger

class XmlPreprocessor:
    def __init__(self):
        self.parser = etree.XMLParser(recover=True, remove_blank_text=False)

    def extract_skeletons(self, xml_text: str, changed_lines: List[int]) -> List[etree._Element]:
        if not xml_text or not changed_lines:
            return []
        try:
            root = etree.fromstring(xml_text.encode('utf-8'), parser=self.parser)
        except Exception as e:
            logger.error(f"XML parse error: {e}")
            return []

        targets = []
        for el in root.iter():
            if isinstance(el.tag, str) and el.sourceline in changed_lines:
                targets.append(el)

        skeletons = []
        processed_paths = set()

        for t_node in targets:
            path = etree.ElementTree(root).getpath(t_node)
            if path in processed_paths:
                continue
            processed_paths.add(path)

            p_node = t_node.getparent()

            if p_node is None:
                skel = etree.Element(t_node.tag, attrib=t_node.attrib, nsmap=t_node.nsmap)
                has_element_children = any(isinstance(c.tag, str) for c in t_node)
                if not has_element_children:
                    skel.text = t_node.text
                    for c in t_node:
                        skel.append(copy.deepcopy(c))
                skeletons.append(skel)
            else:
                skel_p = etree.Element(p_node.tag, attrib=p_node.attrib, nsmap=p_node.nsmap)
                skel_t = etree.Element(t_node.tag, attrib=t_node.attrib, nsmap=t_node.nsmap)
                
                has_element_children = any(isinstance(c.tag, str) for c in t_node)
                if not has_element_children:
                    skel_t.text = t_node.text
                    for c in t_node:
                        skel_t.append(copy.deepcopy(c))
                        
                skel_p.append(skel_t)
                skeletons.append(skel_p)

        return skeletons

    def canonicalize(self, element: etree._Element) -> str:
        el = copy.deepcopy(element)
        etree.strip_tags(el, etree.Comment)
        for node in el.iter():
            if isinstance(node.tag, str):
                attrs = sorted(node.attrib.items())
                node.attrib.clear()
                for k, v in attrs:
                    node.attrib[k] = v
        raw_str = etree.tostring(el, encoding='unicode')
        lines = []
        for line in raw_str.splitlines():
            clean_line = re.sub(r'[ \t]+', ' ', line).strip()
            if clean_line:
                lines.append(clean_line)
        return "\n".join(lines)

    def process(self, old_content: str, new_content: str, old_lns: List[int], new_lns: List[int]) -> List[Dict]:
        old_skeletons = self.extract_skeletons(old_content, old_lns)
        new_skeletons = self.extract_skeletons(new_content, new_lns)
        
        chunks = []
        
        def get_key(node):
            if node is None: return ""
            target = next((c for c in node if isinstance(c.tag, str)), None)
            target_tag = target.tag if target is not None else node.tag
            return f"{node.tag}_{target_tag}"

        paired = []
        used_new = set()
        for o_node in old_skeletons:
            o_key = get_key(o_node)
            best_match_idx = -1
            for i, n_node in enumerate(new_skeletons):
                if i in used_new: continue
                if get_key(n_node) == o_key:
                    best_match_idx = i
                    break
            if best_match_idx != -1:
                paired.append((o_node, new_skeletons[best_match_idx]))
                used_new.add(best_match_idx)
            else:
                paired.append((o_node, None))
                
        for i, n_node in enumerate(new_skeletons):
            if i not in used_new:
                paired.append((None, n_node))
                
        for o_node, n_node in paired:
            raw_old = etree.tostring(o_node, encoding='unicode') if o_node is not None else ""
            clean_old = self.canonicalize(o_node) if o_node is not None else ""
            
            raw_new = etree.tostring(n_node, encoding='unicode') if n_node is not None else ""
            clean_new = self.canonicalize(n_node) if n_node is not None else ""
            
            chunks.append({
                "raw_old_code": raw_old.strip(),
                "clean_old_code": clean_old.strip(),
                "raw_new_code": raw_new.strip(),
                "clean_new_code": clean_new.strip(),
            })
            
        return chunks