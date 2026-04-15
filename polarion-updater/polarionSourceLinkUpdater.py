#!/usr/bin/env python3
"""
Polarion Work Item Manager

This script provides utilities for managing Polarion work items:
1. Update source links by replacing "native" with "SFORD_POS" and "/raw/" with "/blob/"
2. Clear suspect flags on linked work items
3. Print work item descriptions to a file
4. Convert work item descriptions from text/html to text/plain

Environment Variables Required:
- POLARION_API_BASE: Base URL for Polarion API
- POLARION_PAT: Personal Access Token for authentication
- POLARION_PROJECT_ID: Project ID in Polarion (can also be provided via --project-id)

Usage:
    python polarionSourceLinkUpdater.py <query> [--dry-run] [--project-id PROJECT_ID]
    python polarionSourceLinkUpdater.py <query> --clear-suspects [--execute]
    python polarionSourceLinkUpdater.py <query> --convert-descriptions [--execute] [--project-id PROJECT_ID]
    python polarionSourceLinkUpdater.py <query> --jenkins-nth [--execute]
    python polarionSourceLinkUpdater.py --ids ITEM-123 --print-description
    
Examples:
    # Update source links (dry run)
    python polarionSourceLinkUpdater.py "type:testprocedure" --dry-run
    
    # Clear suspect flags on linked work items
    python polarionSourceLinkUpdater.py "type:testprocedure" --clear-suspects --execute
    
    # Convert HTML descriptions to plain text (dry run)
    python polarionSourceLinkUpdater.py "type:testCase" --convert-descriptions --dry-run --project-id MyProject
    
    # Convert HTML descriptions to plain text (execute)
    python polarionSourceLinkUpdater.py --ids SFORD_BL-4049 --convert-descriptions --execute --project-id MyProject
    
    # Replace wassp-jenkins with wassp-jenkins-nth in source links (dry run)
    python polarionSourceLinkUpdater.py "type:testprocedure" --jenkins-nth --dry-run
    
    # Replace wassp-jenkins with wassp-jenkins-nth (execute)
    python polarionSourceLinkUpdater.py --ids SFORD_BSP-19084 --jenkins-nth --execute
    
    # Specify project ID on command line
    python polarionSourceLinkUpdater.py "type:testprocedure" --project-id MyProject --dry-run
"""

import os
import sys
import argparse
import re
import html
import requests
from typing import List, Dict, Any
import json

class PolarionSourceLinkUpdater:
    def __init__(self, base_url: str, pat: str, project_id: str, verify_ssl: bool = False, verbose: bool = False):
        self.base_url = base_url.rstrip('/')
        self.pat = pat
        self.project_id = project_id
        self.verify_ssl = verify_ssl
        self.verbose = verbose
        self.session = requests.Session()
        self.session.headers.update({
            'Authorization': f'Bearer {pat}',
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        })
        self.session.verify = verify_ssl
        
        # Suppress SSL warnings if verification is disabled
        if not verify_ssl:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    
    def query_work_items(self, query: str) -> List[str]:
        """
        Query Polarion for work items matching the query.
        Returns a list of work item IDs.
        
        Uses Lucene query syntax exactly as provided.
        """
        print(f"Querying Polarion with: {query}")
        
        url = f"{self.base_url}/projects/{self.project_id}/workitems"
        
        params = {
            'query': query,
            'fields[workitems]': 'id,type,hyperlinks,title,status'
        }
        
        response = self.session.get(url, params=params, verify=self.verify_ssl)
        
        if self.verbose:
            print(f"  [VERBOSE] Request URL: {response.request.url}")
        
        if response.status_code != 200:
            print(f"Error querying Polarion: {response.status_code}")
            print(f"Response: {response.text[:500]}")
            return []
        
        try:
            data = response.json()
        except json.JSONDecodeError:
            print("Error parsing response from Polarion")
            return []
        
        work_item_ids = []
        
        if isinstance(data, dict) and 'data' in data:
            items = data['data']
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict) and 'id' in item:
                        work_item_ids.append(item['id'])
            else:
                print(f"Unexpected data format: 'data' is not a list")
        elif isinstance(data, dict) and 'links' in data and 'data' not in data:
            print(f"Found 0 work items matching query")
            print(f"  (API endpoint was scoped to project: {self.project_id})")
        else:
            print("Unexpected response format - expected JSON:API structure with 'data' field")
            if isinstance(data, dict):
                print(f"Response keys: {list(data.keys())}")
            print(f"Response preview: {response.text[:1000]}")
        
        print(f"Found {len(work_item_ids)} work items matching query")
        return work_item_ids
    
    def _extract_short_id(self, work_item_id: str) -> str:
        """Extract the short ID from a full work item ID (e.g., 'project/ITEM-123' -> 'ITEM-123')."""
        if '/' in work_item_id:
            return work_item_id.split('/')[-1]
        return work_item_id
    
    def update_work_item_attributes(self, work_item_id: str, attributes: Dict[str, Any], 
                                     dry_run: bool = True, operation_name: str = "update") -> bool:
        """
        Generic method to update any work item attributes.
        
        Args:
            work_item_id: The full or short work item ID
            attributes: Dictionary of attributes to update (e.g., {'status': 'rework'})
            dry_run: If True, don't actually make the change
            operation_name: Name of the operation for logging purposes
            
        Returns:
            True if successful, False otherwise
        """
        if dry_run:
            return True
        
        short_id = self._extract_short_id(work_item_id)
        url = f"{self.base_url}/projects/{self.project_id}/workitems/{short_id}"
        
        payload = {
            'data': {
                'type': 'workitems',
                'id': work_item_id,
                'attributes': attributes
            }
        }
        
        response = self.session.patch(url, json=payload, verify=self.verify_ssl)
        
        if response.status_code in [200, 204]:
            return True
        else:
            print(f"  ✗ Error during {operation_name}: {response.status_code}")
            print(f"    Response: {response.text[:500]}")
            return False
    
    def update_work_item_status(self, work_item_id: str, new_status: str, dry_run: bool = True) -> bool:
        """Update work item status."""
        return self.update_work_item_attributes(
            work_item_id, 
            {'status': new_status}, 
            dry_run=dry_run, 
            operation_name="status update"
        )
    
    def update_work_item_hyperlinks(self, work_item_id: str, updated_hyperlinks: List[Dict[str, Any]], 
                                    dry_run: bool = True) -> bool:
        """Update all hyperlinks for a work item."""
        # Clean up hyperlinks to only include necessary fields
        cleaned_hyperlinks = []
        for link in updated_hyperlinks:
            cleaned_link = {
                'role': link['role'],
                'uri': link['uri']
            }
            # Include other fields if they exist
            if 'id' in link:
                cleaned_link['id'] = link['id']
            cleaned_hyperlinks.append(cleaned_link)
        
        return self.update_work_item_attributes(
            work_item_id, 
            {'hyperlinks': cleaned_hyperlinks}, 
            dry_run=dry_run, 
            operation_name="hyperlinks update"
        )
    
    def process_work_item_ids(self, work_item_ids: List[str], dry_run: bool = True, mode: str = "default"):
        """Process specific work item IDs.
        
        Args:
            mode: 'default' replaces native->SFORD_POS and /raw/->/blob/.
                  'jenkins-nth' replaces wassp-jenkins with wassp-jenkins-nth.
        """
        print(f"Processing {len(work_item_ids)} work item(s) [mode={mode}]")
        
        total_updated = 0
        total_links = 0
        total_items_updated = 0
        items_skipped = 0
        
        for work_item_id in work_item_ids:
            # Get all hyperlinks and the work item data
            short_id = self._extract_short_id(work_item_id)
            
            # Use the correct API endpoint: /projects/{projectId}/workitems/{workItemId}
            url = f"{self.base_url}/projects/{self.project_id}/workitems/{short_id}"
            params = {'fields[workitems]': 'hyperlinks,title,status'}
            
            response = self.session.get(url, params=params, verify=self.verify_ssl)
            
            if response.status_code != 200:
                print(f"\nProcessing: {work_item_id}")
                print(f"  Error getting work item: {response.status_code}")
                print(f"  Attempted URL: {url}")
                continue
            
            data = response.json()
            work_item_data = data.get('data', {})
            attributes = work_item_data.get('attributes', {})
            all_hyperlinks = attributes.get('hyperlinks', [])
            title = attributes.get('title', '')
            status = attributes.get('status', '')
            
            # Track which links need updating
            links_to_update = []
            updated_hyperlinks = []
            
            for link in all_hyperlinks:
                if isinstance(link, dict):
                    role = link.get('role', {})
                    role_id = role.get('id', '') if isinstance(role, dict) else str(role)
                    uri = link.get('uri', '')
                    
                    has_source = 'ref_src' in role_id.lower() or 'source' in role_id.lower()
                    
                    if mode == 'jenkins-nth':
                        # Match wassp-jenkins but NOT wassp-jenkins-nth (already updated)
                        has_jenkins = 'wassp-jenkins' in uri and 'wassp-jenkins-nth' not in uri
                        needs_update = has_source and has_jenkins
                        
                        if self.verbose and has_source:
                            print(f"  [VERBOSE] Link role={role_id}, has_jenkins={has_jenkins}")
                            if not has_jenkins:
                                print(f"    [VERBOSE] URI: {uri[:100]}...")
                    else:
                        has_native = 'native' in uri.lower()
                        has_raw = '/raw/' in uri
                        needs_update = has_source and (has_native or has_raw)
                        
                        if self.verbose and has_source:
                            print(f"  [VERBOSE] Link role={role_id}, native={has_native}, raw={has_raw}")
                            if not has_native and not has_raw:
                                print(f"    [VERBOSE] URI: {uri[:100]}...")
                    
                    if needs_update:
                        # This link needs updating
                        old_uri = uri
                        if mode == 'jenkins-nth':
                            new_uri = uri.replace('wassp-jenkins', 'wassp-jenkins-nth')
                        else:
                            # Replace 'native' with 'SFORD_POS' (case-insensitive)
                            new_uri = uri.replace('native', 'SFORD_POS').replace('Native', 'SFORD_POS').replace('NATIVE', 'SFORD_POS')
                            # Replace 'raw' with 'blob' in the path
                            new_uri = new_uri.replace('/raw/', '/blob/')
                        
                        # Create updated link
                        updated_link = link.copy()
                        updated_link['uri'] = new_uri
                        updated_hyperlinks.append(updated_link)
                        
                        links_to_update.append({
                            'id': link.get('id'),
                            'old_uri': old_uri,
                            'new_uri': new_uri
                        })
                    else:
                        # Keep link as-is
                        updated_hyperlinks.append(link)
            
            if not links_to_update:
                items_skipped += 1
                if self.verbose:
                    print(f"\n[VERBOSE] Skipping: {short_id} - {title}")
                    print(f"  [VERBOSE] Current status: {status}")
                    print(f"  [VERBOSE] Found {len(all_hyperlinks)} total hyperlink(s)")
                    print(f"  [VERBOSE] No source links requiring updates (no 'native' or '/raw/' found)")
                continue
            
            # Only print item header if there are changes to make
            print(f"\nProcessing: {short_id} - {title}")
            print(f"  Current status: {status}")
            print(f"  Found {len(links_to_update)} source link(s) needing updates")
            
            # Step 1: Change status to 'rework'
            if status != 'rework':
                if dry_run:
                    print(f"  [DRY RUN] Would change status from '{status}' to 'rework'")
                else:
                    print(f"  Changing status to 'rework'...")
                    if not self.update_work_item_status(work_item_id, 'rework', dry_run):
                        print(f"  ✗ Failed to change status to 'rework', skipping this item")
                        continue
                    print(f"  ✓ Status changed to 'rework'")
            
            # Show what will be updated
            for link_info in links_to_update:
                if dry_run:
                    print(f"  [DRY RUN] Would update link:")
                    print(f"    Old: {link_info['old_uri']}")
                    print(f"    New: {link_info['new_uri']}")
                    # Highlight the changes
                    if mode == 'jenkins-nth':
                        print(f"    → Replacing 'wassp-jenkins' with 'wassp-jenkins-nth'")
                    else:
                        if 'native' in link_info['old_uri'].lower():
                            print(f"    → Replacing 'native' with 'SFORD_POS'")
                        if '/raw/' in link_info['old_uri']:
                            print(f"    → Replacing '/raw/' with '/blob/'")
                else:
                    print(f"  Updating link:")
                    print(f"    Old: {link_info['old_uri']}")
                    print(f"    New: {link_info['new_uri']}")
                total_links += 1
            
            # Step 2: Update the work item with all hyperlinks (modified + unchanged)
            if self.update_work_item_hyperlinks(work_item_id, updated_hyperlinks, dry_run):
                total_updated += len(links_to_update)
                if not dry_run:
                    total_items_updated += 1
                    print(f"  ✓ Successfully updated work item hyperlinks")
                    # Step 3: Update status to 'in_review'
                    print(f"  Changing status to 'in_review'...")
                    if self.update_work_item_status(work_item_id, 'in_review', dry_run):
                        print(f"  ✓ Successfully updated status to 'in_review'")
                    else:
                        print(f"  ⚠ Hyperlinks updated but failed to update status to 'in_review'")
                else:
                    print(f"  [DRY RUN] Would then change status to 'in_review'")
            elif not dry_run:
                print(f"  ✗ Failed to update work item hyperlinks")
        
        print(f"\n{'=' * 60}")
        print(f"Summary:")
        print(f"  Total work items processed: {len(work_item_ids)}")
        print(f"  Items skipped (no changes needed): {items_skipped}")
        print(f"  Total links to update: {total_links}")
        if dry_run:
            print(f"  Links that would be updated: {total_updated}")
            print(f"\nThis was a DRY RUN. Use --execute to apply changes.")
        else:
            print(f"  Links successfully updated: {total_updated}")
            print(f"  Work items successfully updated: {total_items_updated}")
        print(f"{'=' * 60}")
    
    def print_work_item_descriptions(self, work_item_ids: List[str], output_file: str = "workitem_descriptions.txt"):
        """
        Print work item titles and descriptions to a text file.
        
        Args:
            work_item_ids: List of work item IDs to process
            output_file: Path to the output file (default: workitem_descriptions.txt)
        """
        print(f"Fetching descriptions for {len(work_item_ids)} work item(s)...")
        
        entries = []
        
        for work_item_id in work_item_ids:
            short_id = self._extract_short_id(work_item_id)
            
            url = f"{self.base_url}/projects/{self.project_id}/workitems/{short_id}"
            params = {'fields[workitems]': 'title,description'}
            
            if self.verbose:
                print(f"  [VERBOSE] GET {url}")
            
            response = self.session.get(url, params=params, verify=self.verify_ssl)
            
            if response.status_code != 200:
                print(f"  Error getting work item {work_item_id}: {response.status_code}")
                continue
            
            data = response.json()
            attributes = data.get('data', {}).get('attributes', {})
            title = attributes.get('title', 'No title')
            description_raw = attributes.get('description', {}).get('value', 'No description')
            
            # Strip HTML tags from description
            description = re.sub(r'<[^>]+>', '', description_raw)
            # Clean up extra whitespace and normalize line breaks
            description = re.sub(r'\s+', ' ', description).strip()
            
            entries.append({
                'title': title,
                'description': description
            })
            if self.verbose:
                print(f"  ✓ {short_id}: {title[:50]}..." if len(title) > 50 else f"  ✓ {short_id}: {title}")
        
        # Write to file
        with open(output_file, 'w', encoding='utf-8') as f:
            for entry in entries:
                f.write("/*\n")
                f.write(f"{entry['title']}\n")
                f.write(f"{entry['description']}\n")
                f.write("*/\n\n")
        
        print(f"\n✓ Wrote {len(entries)} work item(s) to {output_file}")
    
    def get_linked_work_items(self, work_item_id: str) -> List[Dict[str, Any]]:
        """
        Get all linked work items for a specific work item.
        Uses the /projects/{projectId}/workitems/{workItemId}/linkedworkitems endpoint.
        
        Returns a list of linked work item objects with their attributes.
        """
        short_id = self._extract_short_id(work_item_id)
        url = f"{self.base_url}/projects/{self.project_id}/workitems/{short_id}/linkedworkitems"
        
        # Request all fields for linkedworkitems
        params = {
            'fields[linkedworkitems]': '@all'
        }
        
        if self.verbose:
            print(f"  [VERBOSE] GET {url}")
            print(f"  [VERBOSE] Params: {params}")
        
        response = self.session.get(url, params=params, verify=self.verify_ssl)
        
        if self.verbose:
            print(f"  [VERBOSE] Response status: {response.status_code}")
        
        if response.status_code != 200:
            print(f"  Error getting linked work items: {response.status_code}")
            if self.verbose:
                print(f"  [VERBOSE] Response: {response.text[:500]}")
            return []
        
        try:
            data = response.json()
            return data.get('data', [])
        except json.JSONDecodeError:
            print("  Error parsing linked work items response")
            return []
    
    def update_linked_work_item_suspect(self, work_item_id: str, url_suffix: str, 
                                         full_link_id: str, suspect: bool, dry_run: bool = True) -> bool:
        """
        Update the suspect flag on a linked work item.
        
        Args:
            work_item_id: The source work item ID
            url_suffix: The suffix for the URL (e.g., implements/Shallowford_BL/SFORD_BL-1475)
            full_link_id: The full linked work item ID for the payload
            suspect: The new suspect value
            dry_run: If True, don't actually make the change
        """
        if dry_run:
            return True
        
        short_id = self._extract_short_id(work_item_id)
        
        url = f"{self.base_url}/projects/{self.project_id}/workitems/{short_id}/linkedworkitems/{url_suffix}"
        
        payload = {
            'data': {
                'type': 'linkedworkitems',
                'id': full_link_id,
                'attributes': {
                    'suspect': suspect
                }
            }
        }
        
        if self.verbose:
            print(f"      [VERBOSE] PATCH {url}")
            print(f"      [VERBOSE] Payload: {json.dumps(payload)}")
        
        response = self.session.patch(url, json=payload, verify=self.verify_ssl)
        
        if self.verbose:
            print(f"      [VERBOSE] Response status: {response.status_code}")
        
        if response.status_code in [200, 204]:
            return True
        else:
            print(f"    ✗ Error updating suspect flag: {response.status_code}")
            print(f"      Response: {response.text[:300]}")
            return False
    
    def process_suspect_links(self, work_item_ids: List[str], dry_run: bool = True):
        """
        Process work items and clear suspect flags on their linked work items.
        
        Args:
            work_item_ids: List of work item IDs to process
            dry_run: If True, only report suspect links without clearing them
        """
        print(f"Processing suspect links for {len(work_item_ids)} work item(s)")
        
        total_suspects_found = 0
        total_suspects_cleared = 0
        items_with_suspects = 0
        items_skipped = 0
        
        for work_item_id in work_item_ids:
            short_id = self._extract_short_id(work_item_id)
            
            # First get work item title and status for better output
            url = f"{self.base_url}/projects/{self.project_id}/workitems/{short_id}"
            params = {'fields[workitems]': 'title,status'}
            response = self.session.get(url, params=params, verify=self.verify_ssl)
            
            title = ""
            status = ""
            if response.status_code == 200:
                data = response.json()
                attributes = data.get('data', {}).get('attributes', {})
                title = attributes.get('title', '')
                status = attributes.get('status', '')
            
            # Get linked work items
            linked_items = self.get_linked_work_items(work_item_id)
            
            if not linked_items:
                items_skipped += 1
                if self.verbose:
                    print(f"\n[VERBOSE] Skipping: {short_id} - {title}")
                    print(f"  [VERBOSE] No linked work items found")
                continue
            
            # Find suspect links
            suspect_links = []
            for item in linked_items:
                if isinstance(item, dict):
                    item_id = item.get('id', '')
                    attributes = item.get('attributes', {})
                    role = attributes.get('role', 'unknown')
                    suspect_raw = attributes.get('suspect', False)
                    # Handle both boolean and string representations
                    suspect = suspect_raw if isinstance(suspect_raw, bool) else str(suspect_raw).lower() == 'true'
                    
                    # Get the target work item ID from relationships
                    relationships = item.get('relationships', {})
                    target_work_item = relationships.get('workItem', {}).get('data', {})
                    target_id = target_work_item.get('id', 'unknown') if target_work_item else 'unknown'
                    
                    # Debug output when verbose
                    if self.verbose:
                        print(f"    [VERBOSE] Link: role={role}, suspect={suspect_raw} (parsed as {suspect}), target={self._extract_short_id(target_id)}")
                    
                    if suspect:
                        suspect_links.append({
                            'id': item_id,
                            'role': role,
                            'target_id': target_id
                        })
            
            if not suspect_links:
                items_skipped += 1
                if self.verbose:
                    print(f"\n[VERBOSE] Skipping: {short_id} - {title}")
                    print(f"  [VERBOSE] Current status: {status}")
                    print(f"  [VERBOSE] Found {len(linked_items)} linked work item(s)")
                    print(f"  [VERBOSE] No suspect links found")
                continue
            
            # Only print item header if there are suspect links
            print(f"\nProcessing: {short_id} - {title}")
            print(f"  Current status: {status}")
            print(f"  Found {len(linked_items)} linked work item(s)")
            
            items_with_suspects += 1
            total_suspects_found += len(suspect_links)
            print(f"  Found {len(suspect_links)} suspect link(s):")
            
            # Step 1: Change status to 'rework' before clearing suspects
            if dry_run:
                print(f"  [DRY RUN] Would change status from '{status}' to 'rework'")
            else:
                if status != 'rework':
                    print(f"  Changing status to 'rework'...")
                    if not self.update_work_item_status(work_item_id, 'rework', dry_run=False):
                        print(f"  ✗ Failed to change status to 'rework', skipping this item")
                        continue
                    print(f"  ✓ Status changed to 'rework'")
            
            # Step 2: Clear suspect flags
            suspects_cleared_for_item = 0
            for link in suspect_links:
                target_short_id = self._extract_short_id(link['target_id'])
                if dry_run:
                    print(f"    [DRY RUN] Would clear suspect on: {link['role']} -> {target_short_id}")
                else:
                    print(f"    Clearing suspect on: {link['role']} -> {target_short_id}")
                    # Extract just the link-specific part of the ID for the URL
                    # The ID format is: project/WORKITEM/role/project/TARGET
                    # URL needs: role/project/TARGET
                    # Payload needs: full ID
                    full_link_id = link['id']
                    link_id_parts = link['id'].split('/')
                    if len(link_id_parts) >= 4:
                        # Reconstruct as: role/project/TARGET (e.g., implements/Shallowford_BL/SFORD_BL-1475)
                        link_suffix = '/'.join(link_id_parts[2:])
                    else:
                        link_suffix = link['id']
                    
                    if self.update_linked_work_item_suspect(work_item_id, link_suffix, full_link_id, False, dry_run=False):
                        total_suspects_cleared += 1
                        suspects_cleared_for_item += 1
                        print(f"      ✓ Suspect flag cleared")
                    else:
                        print(f"      ✗ Failed to clear suspect flag")
            
            # Step 3: Change status to 'in_review' after clearing suspects
            if dry_run:
                print(f"  [DRY RUN] Would change status to 'in_review'")
            else:
                if suspects_cleared_for_item > 0:
                    print(f"  Changing status to 'in_review'...")
                    if self.update_work_item_status(work_item_id, 'in_review', dry_run=False):
                        print(f"  ✓ Status changed to 'in_review'")
                    else:
                        print(f"  ⚠ Suspects cleared but failed to change status to 'in_review'")
        
        # Summary
        print(f"\n{'=' * 60}")
        print(f"Suspect Links Summary:")
        print(f"  Total work items processed: {len(work_item_ids)}")
        print(f"  Items skipped (no suspect links): {items_skipped}")
        print(f"  Work items with suspect links: {items_with_suspects}")
        print(f"  Total suspect links found: {total_suspects_found}")
        if dry_run:
            print(f"\nThis was a DRY RUN. Use --execute to clear suspect flags.")
        else:
            print(f"  Suspect links cleared: {total_suspects_cleared}")
        print(f"{'=' * 60}")

    def _strip_html(self, html_text: str) -> str:
        """
        Convert HTML text to plain text by stripping tags and decoding entities.
        """
        # Remove HTML tags
        text = re.sub(r'<br\s*/?>', '\n', html_text)  # Convert <br> to newlines
        text = re.sub(r'<[^>]+>', '', text)  # Strip remaining tags
        # Decode HTML entities (e.g., &amp; -> &, &lt; -> <)
        text = html.unescape(text)
        # Normalize whitespace within lines but preserve line breaks
        lines = text.split('\n')
        lines = [re.sub(r'[ \t]+', ' ', line).strip() for line in lines]
        # Remove empty lines at start/end, collapse multiple empty lines
        text = '\n'.join(lines).strip()
        text = re.sub(r'\n{3,}', '\n\n', text)
        return text

    def process_description_conversion(self, work_item_ids: List[str], dry_run: bool = True):
        """
        Convert work item descriptions from text/html to text/plain.
        Strips HTML tags and entities, keeping only the text content.
        
        Args:
            work_item_ids: List of work item IDs to process
            dry_run: If True, only report what would change without applying
        """
        print(f"Processing description conversion for {len(work_item_ids)} work item(s)")
        
        total_converted = 0
        items_skipped = 0
        items_failed = 0
        
        for work_item_id in work_item_ids:
            short_id = self._extract_short_id(work_item_id)
            
            url = f"{self.base_url}/projects/{self.project_id}/workitems/{short_id}"
            params = {'fields[workitems]': 'title,description,status'}
            
            response = self.session.get(url, params=params, verify=self.verify_ssl)
            
            if response.status_code != 200:
                print(f"\n  Error getting work item {short_id}: {response.status_code}")
                items_failed += 1
                continue
            
            data = response.json()
            attributes = data.get('data', {}).get('attributes', {})
            title = attributes.get('title', '')
            status = attributes.get('status', '')
            description = attributes.get('description', {})
            desc_type = description.get('type', '') if isinstance(description, dict) else ''
            desc_value = description.get('value', '') if isinstance(description, dict) else ''
            
            # Skip if already text/plain or no description
            if desc_type != 'text/html':
                items_skipped += 1
                if self.verbose:
                    print(f"\n[VERBOSE] Skipping: {short_id} - {title}")
                    print(f"  [VERBOSE] Description type is '{desc_type}', not 'text/html'")
                continue
            
            plain_text = self._strip_html(desc_value)
            
            print(f"\nProcessing: {short_id} - {title}")
            print(f"  Current status: {status}")
            print(f"  Description type: {desc_type}")
            
            if dry_run:
                if status != 'rework':
                    print(f"  [DRY RUN] Would change status from '{status}' to 'rework'")
                print(f"  [DRY RUN] Would convert description from text/html to text/plain")
                print(f"    HTML:  {desc_value[:150]}{'...' if len(desc_value) > 150 else ''}")
                print(f"    Plain: {plain_text[:150]}{'...' if len(plain_text) > 150 else ''}")
                print(f"  [DRY RUN] Would change status to 'in_review'")
                total_converted += 1
            else:
                # Step 1: Change status to 'rework'
                if status != 'rework':
                    print(f"  Changing status to 'rework'...")
                    if not self.update_work_item_status(work_item_id, 'rework', dry_run=False):
                        print(f"  ✗ Failed to change status to 'rework', skipping this item")
                        items_failed += 1
                        continue
                    print(f"  ✓ Status changed to 'rework'")
                
                # Step 2: Update description
                new_description = {
                    'type': 'text/plain',
                    'value': plain_text
                }
                if self.update_work_item_attributes(
                    work_item_id,
                    {'description': new_description},
                    dry_run=False,
                    operation_name="description conversion"
                ):
                    total_converted += 1
                    print(f"  ✓ Description converted to text/plain")
                    
                    # Step 3: Change status to 'in_review'
                    print(f"  Changing status to 'in_review'...")
                    if self.update_work_item_status(work_item_id, 'in_review', dry_run=False):
                        print(f"  ✓ Status changed to 'in_review'")
                    else:
                        print(f"  ⚠ Description converted but failed to change status to 'in_review'")
                else:
                    print(f"  ✗ Failed to convert description")
                    items_failed += 1
        
        # Summary
        print(f"\n{'=' * 60}")
        print(f"Description Conversion Summary:")
        print(f"  Total work items processed: {len(work_item_ids)}")
        print(f"  Items skipped (already text/plain): {items_skipped}")
        if dry_run:
            print(f"  Descriptions that would be converted: {total_converted}")
            print(f"\nThis was a DRY RUN. Use --execute to apply changes.")
        else:
            print(f"  Descriptions converted: {total_converted}")
            print(f"  Items failed: {items_failed}")
        print(f"{'=' * 60}")


def _resolve_work_item_ids(args, project_id, updater) -> List[str]:
    """Resolve work item IDs from --ids, --ids-file, or query."""
    if args.ids:
        return [f"{project_id}/{wid}" if '/' not in wid else wid for wid in args.ids]
    elif args.ids_file:
        try:
            with open(args.ids_file, 'r') as f:
                content = f.read()
                ids = [id.strip() for id in content.split() if id.strip() and not id.strip().startswith('#')]
            print(f"Loaded {len(ids)} work item IDs from {args.ids_file}")
            return [f"{project_id}/{wid}" if '/' not in wid else wid for wid in ids]
        except FileNotFoundError:
            print(f"Error: File not found: {args.ids_file}")
            sys.exit(1)
    elif args.query:
        return updater.query_work_items(args.query)
    return []


def main():
    parser = argparse.ArgumentParser(
        description='Update Polarion source links by replacing "native" with "SFORD_POS" and "/raw/" with "/blob/"',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s "type:testprocedure" --dry-run
  %(prog)s "type:testprocedure AND status:approved" --execute
  %(prog)s --ids SFORD_BSP-19084 --execute
  %(prog)s --ids-file workitems.txt --dry-run
  %(prog)s "type:testprocedure" --clear-suspects --dry-run
  %(prog)s --ids SFORD_BL-3387 --clear-suspects --execute
  %(prog)s "type:testCase" --convert-descriptions --dry-run
  %(prog)s --ids SFORD_BL-4049 --convert-descriptions --execute
  %(prog)s "type:testprocedure" --jenkins-nth --dry-run
  %(prog)s --ids SFORD_BSP-19084 --jenkins-nth --execute
        """
    )
    
    parser.add_argument(
        'query',
        nargs='?',
        help='Polarion Lucene query string (e.g., "type:testprocedure AND status:approved")'
    )
    
    parser.add_argument(
        '--ids',
        nargs='+',
        help='List of work item IDs to process (e.g., SFORD_BSP-19064 SFORD_BSP-23493)'
    )
    
    parser.add_argument(
        '--ids-file',
        help='File containing work item IDs (one per line)'
    )
    
    parser.add_argument(
        '--dry-run',
        action='store_true',
        default=True,
        help='Show what would be changed without making actual changes (default)'
    )
    
    parser.add_argument(
        '--execute',
        action='store_true',
        help='Actually execute the changes (overrides --dry-run)'
    )
    
    parser.add_argument(
        '--verify-ssl',
        action='store_true',
        default=False,
        help='Enable SSL certificate verification (disabled by default for corporate environments)'
    )
    
    parser.add_argument(
        '--print-description',
        metavar='OUTPUT_FILE',
        nargs='?',
        const='workitem_descriptions.txt',
        help='Print work item titles and descriptions to a file (default: workitem_descriptions.txt)'
    )
    
    parser.add_argument(
        '--project-id',
        help='Polarion project ID (overrides POLARION_PROJECT_ID environment variable)'
    )
    
    parser.add_argument(
        '--clear-suspects',
        action='store_true',
        help='Find and clear suspect flags on linked work items'
    )
    
    parser.add_argument(
        '--convert-descriptions',
        action='store_true',
        help='Convert work item descriptions from text/html to text/plain'
    )
    
    parser.add_argument(
        '--jenkins-nth',
        action='store_true',
        help='Replace wassp-jenkins with wassp-jenkins-nth in source links'
    )
    
    parser.add_argument(
        '-v', '--verbose',
        action='store_true',
        help='Enable verbose output for debugging'
    )
    
    args = parser.parse_args()
    
    # Validate that either query, ids, or ids-file is provided
    if not args.query and not args.ids and not args.ids_file:
        parser.error("Either query, --ids, or --ids-file must be provided")
    
    # Get environment variables
    base_url = os.environ.get('POLARION_API_BASE')
    pat = os.environ.get('POLARION_PAT')
    # Project ID can come from command line or environment variable
    project_id = args.project_id or os.environ.get('POLARION_PROJECT_ID')
    
    # Validate required configuration
    missing_vars = []
    if not base_url:
        missing_vars.append('POLARION_API_BASE')
    if not pat:
        missing_vars.append('POLARION_PAT')
    if not project_id:
        missing_vars.append('POLARION_PROJECT_ID (or use --project-id)')
    
    if missing_vars:
        print("Error: Missing required environment variables:")
        for var in missing_vars:
            print(f"  - {var}")
        print("\nPlease set these variables before running the script.")
        sys.exit(1)
    
    # Determine if this is a dry run
    dry_run = not args.execute
    
    if dry_run:
        print("=" * 60)
        print("DRY RUN MODE - No changes will be made")
        print("=" * 60)
    else:
        print("=" * 60)
        print("EXECUTE MODE - Changes will be applied!")
        print("=" * 60)
    
    # Create updater and process
    updater = PolarionSourceLinkUpdater(base_url, pat, project_id, verify_ssl=args.verify_ssl, verbose=args.verbose)
    
    # Resolve work item IDs from query, --ids, or --ids-file
    formatted_ids = _resolve_work_item_ids(args, project_id, updater)
    
    if not formatted_ids:
        print("No work items found.")
        return
    
    # Handle --print-description mode
    if args.print_description:
        updater.print_work_item_descriptions(formatted_ids, args.print_description)
    # Handle --convert-descriptions mode
    elif args.convert_descriptions:
        updater.process_description_conversion(formatted_ids, dry_run=dry_run)
    # Handle --clear-suspects mode
    elif args.clear_suspects:
        updater.process_suspect_links(formatted_ids, dry_run=dry_run)
    # jenkins-nth mode: replace wassp-jenkins with wassp-jenkins-nth
    elif args.jenkins_nth:
        updater.process_work_item_ids(formatted_ids, dry_run=dry_run, mode='jenkins-nth')
    # Default: update source links
    else:
        updater.process_work_item_ids(formatted_ids, dry_run=dry_run)


if __name__ == '__main__':
    main()
