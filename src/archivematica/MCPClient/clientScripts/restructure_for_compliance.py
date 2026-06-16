#!/usr/bin/env python
# This file is part of Archivematica.
#
# Copyright 2010-2013 Artefactual Systems Inc. <http://artefactual.com>
#
# Archivematica is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Archivematica is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Archivematica.  If not, see <http://www.gnu.org/licenses/>.
import errno
import os
import re
import shutil
import time
import uuid

import django

django.setup()
from django.core.exceptions import ValidationError
from django.db import transaction

from archivematica.archivematicaCommon import bag
from archivematica.archivematicaCommon.archivematicaFunctions import OPTIONAL_FILES
from archivematica.archivematicaCommon.archivematicaFunctions import REQUIRED_DIRECTORIES
from archivematica.archivematicaCommon.archivematicaFunctions import create_structured_directory
from archivematica.archivematicaCommon.archivematicaFunctions import reconstruct_empty_directories
from archivematica.archivematicaCommon.custom_handlers import get_script_logger
from archivematica.dashboard.main.models import PACKAGE_STATUS_FAILED
from archivematica.dashboard.main.models import SIP
from archivematica.dashboard.main.models import Transfer

logger = get_script_logger("archivematica.mcp.client.restructureForCompliance")


def _move_file(job, src, dst, exit_on_error=True):
    logger.info("Moving %s to %s", src, dst)
    try:
        shutil.move(src, dst)
        job.pyprint(f"Moved: {src} -> {dst}")
    except OSError:
        job.pyprint(f"Could not move {src}")
        if exit_on_error:
            raise


def _create_sip_backup(job, sip_path):
    """Create a filesystem snapshot (copy) of sip_path to allow rollback.

    Returns the path to the backup directory or None if backup failed.
    """
    try:
        sip_abs = os.path.abspath(sip_path)
        sip_norm = sip_abs.rstrip(os.sep)
        parent_dir = os.path.dirname(sip_norm)
        base = os.path.basename(sip_norm)
        backup_dir = os.path.join(parent_dir, f"{base}.pre_restructure_backup_{uuid.uuid4()}")
        if os.path.exists(backup_dir):
            backup_dir = f"{backup_dir}_{int(time.time())}"
        job.pyprint(f"Creating SIP backup at {backup_dir}")
        shutil.copytree(sip_norm, backup_dir, copy_function=shutil.copy2)
        return backup_dir
    except Exception as exc:
        job.pyprint(f"Could not create SIP backup: {exc}; proceeding without backup")
        logger.exception("SIP backup failed for %s", sip_path)
        return None


def _restore_sip_backup(job, sip_path, backup_dir):
    """Restore the backup by replacing sip_path with backup_dir."""
    try:
        job.pyprint(f"Restoring SIP from backup {backup_dir} to {sip_path}")
        if os.path.exists(sip_path):
            shutil.rmtree(sip_path)
        shutil.move(backup_dir, sip_path)
        job.pyprint(f"SIP restored from backup {backup_dir}")
        return True
    except Exception as exc:
        job.pyprint(f"Failed to restore SIP from backup: {exc}")
        logger.exception("Failed to restore SIP from %s to %s", backup_dir, sip_path)
        return False


def _cleanup_sip_backup(job, backup_dir):
    try:
        if backup_dir and os.path.exists(backup_dir):
            job.pyprint(f"Removing SIP backup {backup_dir}")
            shutil.rmtree(backup_dir)
    except Exception:
        logger.exception("Failed to remove SIP backup %s", backup_dir)


def restructure_transfer_aip(job, unit_path):
    """
    Restructure a transfer that comes from re-ingesting an Archivematica AIP.
    """
    old_bag = os.path.join(unit_path, "old_bag", "")
    os.makedirs(old_bag)

    # Move everything to old_bag
    for item in os.listdir(unit_path):
        if item == "old_bag":
            continue
        src = os.path.join(unit_path, item)
        _move_file(job, src, old_bag)

    # Create required directories
    # - "/logs" and "/logs/fileMeta"
    # - "/metadata" and "/metadata/submissionDocumentation"
    # - "/objects"
    create_structured_directory(unit_path, printing=True, printfn=job.pyprint)

    # Move /old_bag/data/METS.<UUID>.xml => /metadata/METS.<UUID>.xml
    p = re.compile(r"^METS\..*\.xml$", re.IGNORECASE)
    src = os.path.join(old_bag, "data")
    m = None
    for item in os.listdir(src):
        m = p.match(item)
        if m:
            break  # Stop trying after the first match
    if not m:
        raise FileNotFoundError(
            f"Could not find METS XML file in {src}"
        )
    src = os.path.join(src, m.group())
    dst = os.path.join(unit_path, "metadata")
    # After moving the METS file into the metadata directory, mets_file_path
    # should reference the actual METS file path inside the metadata dir.
    mets_file_path = os.path.join(dst, m.group())
    _move_file(job, src, dst)

    # Move /old_bag/data/objects/metadata/* => /metadata/
    src = os.path.join(old_bag, "data", "objects", "metadata")
    dst = os.path.join(unit_path, "metadata")
    if os.path.isdir(src):
        for item in os.listdir(src):
            item_path = os.path.join(src, item)
            _move_file(job, item_path, dst)
        shutil.rmtree(src)

    # Move /old_bag/data/objects/submissionDocumentation/* => /metadata/submissionDocumentation/
    src = os.path.join(old_bag, "data", "objects", "submissionDocumentation")
    dst = os.path.join(unit_path, "metadata", "submissionDocumentation")
    if os.path.isdir(src):
        for item in os.listdir(src):
            item_path = os.path.join(src, item)
            _move_file(job, item_path, dst)
        shutil.rmtree(src)

    # Move /old_bag/data/objects/* => /objects/
    src = os.path.join(old_bag, "data", "objects")
    objects_path = dst = os.path.join(unit_path, "objects")
    for item in os.listdir(src):
        item_path = os.path.join(src, item)
        _move_file(job, item_path, dst)

    # Move /old_bag/processingMCP.xml => /processingMCP.xml
    src = os.path.join(old_bag, "processingMCP.xml")
    dst = os.path.join(unit_path, "processingMCP.xml")
    if os.path.isfile(src):
        _move_file(job, src, dst)

    # Get rid of old_bag
    shutil.rmtree(old_bag)

    # Reconstruct any empty directories documented in the METS file under the
    # logical structMap labelled "Normative Directory Structure"
    reconstruct_empty_directories(mets_file_path, objects_path, logger=logger)


def restructure_transfer(job, unit_path):
    # Create required directories
    create_structured_directory(unit_path, printing=True, printfn=job.pyprint)

    # Move everything else to the objects directory
    for item in os.listdir(unit_path):
        src = os.path.join(unit_path, item)
        dst = os.path.join(unit_path, "objects", ".")
        if os.path.isdir(src) and item not in REQUIRED_DIRECTORIES:
            _move_file(job, src, dst)
        elif os.path.isfile(src) and item not in OPTIONAL_FILES:
            _move_file(job, src, dst)


def _is_bag_structure(unit_path):
    """Return True if the directory looks like a BagIt bag (has bagit.txt or data/)."""
    return os.path.isfile(os.path.join(unit_path, "bagit.txt")) or os.path.isdir(
        os.path.join(unit_path, "data")
    )


def _fallback_physical_restructure(job, unit_path):
    """Perform a best-effort filesystem-only restructure into the compliance
    directory layout. This does not update the Dashboard DB; it moves files
    and directories into the standard `objects`, `metadata`, `logs`, etc.

    This fallback is used when DB-driven moves fail (missing DB records).
    """
    # Create required directories (safe if they already exist)
    create_structured_directory(unit_path, manual_normalization=True, printing=False)

    unit_path = os.path.join(unit_path, "")
    objects_path = os.path.join(unit_path, "objects")

    # Move top-level files into objects or metadata/submissionDocumentation
    for entry in os.listdir(unit_path):
        if entry in OPTIONAL_FILES or entry in REQUIRED_DIRECTORIES:
            continue
        src = os.path.join(unit_path, entry)
        if os.path.isfile(src):
            # Decide destination: manifest-like to metadata, others to objects
            if entry.startswith("manifest") or entry.endswith(".xml"):
                dst_dir = os.path.join(unit_path, "metadata")
            else:
                dst_dir = objects_path
            dst = os.path.join(dst_dir, entry)
            try:
                os.replace(src, dst)
                job.pyprint(f"Moved (fallback): {src} -> {dst}")
            except OSError as exc:
                # If destination exists, try to unlink and replace; otherwise raise
                if exc.errno == errno.EEXIST:
                    try:
                        os.remove(dst)
                        os.replace(src, dst)
                        job.pyprint(f"Replaced existing (fallback): {dst}")
                    except Exception:
                        job.pyprint(f"Fallback move failed for {src}: {exc}")
                        raise
                else:
                    job.pyprint(f"Fallback move failed for {src}: {exc}")
                    raise
        elif os.path.isdir(src):
            # Move directories except required ones into objects preserving name
            if entry in REQUIRED_DIRECTORIES:
                continue
            dst = os.path.join(objects_path, entry)
            try:
                # If dst exists, merge contents
                if os.path.isdir(dst):
                    for root, dirs, files in os.walk(src):
                        rel = os.path.relpath(root, src)
                        target_root = os.path.join(dst, rel) if rel != "." else dst
                        os.makedirs(target_root, exist_ok=True)
                        for f in files:
                            s_f = os.path.join(root, f)
                            d_f = os.path.join(target_root, f)
                            if os.path.exists(d_f):
                                os.remove(d_f)
                            os.replace(s_f, d_f)
                    shutil.rmtree(src)
                else:
                    os.replace(src, dst)
                job.pyprint(f"Moved dir (fallback): {src} -> {dst}")
            except Exception as exc:
                job.pyprint(f"Fallback move dir failed for {src}: {exc}")
                raise

    # Ensure submissionDocumentation exists
    subm = os.path.join(unit_path, "metadata", "submissionDocumentation")
    os.makedirs(subm, exist_ok=True)
    job.pyprint("Filesystem-only fallback restructure completed")


def call(jobs):
    with transaction.atomic():
        for job in jobs:
            # Capture sip_uuid early so we can act on failures after JobContext
            try:
                sip_uuid = job.args[2]
            except Exception:
                sip_uuid = None

            # Capture sip_path early so we can create a filesystem backup before
            # entering the JobContext.
            try:
                sip_path = job.args[1]
            except Exception:
                sip_path = None

            backup_dir = None
            if sip_path:
                try:
                    backup_dir = _create_sip_backup(job, sip_path)
                except Exception:
                    logger.exception("Failed to create SIP backup for %s", sip_path)
                    backup_dir = None

            with job.JobContext(logger=logger):
                try:
                    # Ensure sip_path is available inside the JobContext
                    if not sip_path:
                        sip_path = job.args[1]

                    transfer = None
                    sip = None
                    try:
                        transfer = Transfer.objects.get(uuid=sip_uuid)
                    except (Transfer.DoesNotExist, ValidationError):
                        sip = SIP.objects.get(uuid=sip_uuid)

                    if transfer:
                        logger.info("Transfer.type=%s", transfer.type)
                    else:
                        logger.info("SIP.sip_type=%s", sip.sip_type)

                    if transfer and transfer.type == "Archivematica AIP":
                        logger.info("Archivematica AIP detected, verifying bag...")
                        if not bag.is_valid(sip_path, job.pyprint):
                            logger.info("Archivematica AIP: bag verification failed!")
                            job.set_status(1)
                            continue

                        if not _is_bag_structure(sip_path):
                            logger.info(
                                "Transfer at %s already has compliance structure, skipping restructure.",
                                sip_path,
                            )
                            # Clean up any leftover old_bag/ from a previous failed restructure attempt.
                            old_bag_path = os.path.join(sip_path, "old_bag")
                            if os.path.isdir(old_bag_path):
                                logger.info("Removing leftover old_bag/ at %s", old_bag_path)
                                shutil.rmtree(old_bag_path)
                        else:
                            job.pyprint("Restructuring transfer (Archivematica AIP re-ingest)...")
                            try:
                                restructure_transfer_aip(job, sip_path)
                            except Exception as exc:
                                logger.exception(
                                    "restructure_transfer_aip failed, attempting filesystem fallback: %s",
                                    exc,
                                )
                                job.pyprint("Restructure (AIP) failed, attempting filesystem-only fallback.")
                                try:
                                    _fallback_physical_restructure(job, sip_path)
                                except Exception:
                                    logger.exception("Filesystem fallback also failed")
                                    raise
                    else:
                        job.pyprint("Restructuring transfer...")
                        try:
                            restructure_transfer(job, sip_path)
                        except Exception as exc:
                            logger.exception(
                                "restructure_transfer failed, attempting filesystem fallback: %s",
                                exc,
                            )
                            job.pyprint("Restructure failed, attempting filesystem-only fallback.")
                            try:
                                _fallback_physical_restructure(job, sip_path)
                            except Exception:
                                logger.exception("Filesystem fallback also failed")
                                raise

                except OSError as err:
                    job.pyprint(repr(err))
                    job.set_status(1)

            # End of JobContext: if job failed, attempt to restore the SIP from backup
            exit_code = job.get_exit_code()
            if exit_code and exit_code != 0:
                if backup_dir:
                    try:
                        job.pyprint(f"Restoring SIP from backup due to job failure: {backup_dir}")
                        _restore_sip_backup(job, sip_path, backup_dir)
                    except Exception:
                        logger.exception("Failed to restore SIP from backup for %s", sip_path)
                else:
                    job.pyprint(f"No SIP backup available to restore for {sip_path}")

                try:
                    if sip_uuid:
                        try:
                            transfer = Transfer.objects.filter(uuid=sip_uuid).first()
                            if transfer:
                                transfer.status = PACKAGE_STATUS_FAILED
                                transfer.save()
                            else:
                                sip = SIP.objects.filter(uuid=sip_uuid).first()
                                if sip:
                                    sip.status = PACKAGE_STATUS_FAILED
                                    sip.save()
                        except Exception:
                            logger.exception("Failed to update unit status after job failure")
                finally:
                    try:
                        job.update_task_status()
                    except Exception:
                        logger.exception("Failed to update Task status for failed job")
                    raise RuntimeError("Aborting processing due to job failure")
            else:
                # Job succeeded: clean up the filesystem backup if present
                if backup_dir:
                    try:
                        _cleanup_sip_backup(job, backup_dir)
                    except Exception:
                        logger.exception("Failed to cleanup SIP backup %s", backup_dir)
