#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Unit tests for the various components associated with PID (persistent
identifier binding and declaration in Archivematica.
"""
from __future__ import unicode_literals
import os
import sys

from django.core.management import call_command
import pytest


from job import Job



THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.abspath(os.path.join(THIS_DIR, "../lib/clientScripts")))


import bind_pids


class TestPIDComponents(object):
    """PID binding and declaration test runner class."""

    fixture_files = ["agents.json", "sip.json", "files.json", "events-transfer.json"]
    fixtures = [os.path.join(THIS_DIR, "fixtures", p) for p in fixture_files]

    job = Job("stub", "stub", [])

    def test_something(self):
        assert True

    def test_bind_pids_no_settings(self):
        """Test the output of the code without any args.

        bind_pids should return zero, for no-error. It won't have performed
        any actions on the database either.
        """
        assert bind_pids.main(self.job, None, None, None) == 0

    @pytest.mark.django_db
    def test_bind_pids_no_config(self):
        """Test the output of the code without any args.

        In this instance, we want bind_pids to thing that there is some
        configuration available but we haven't provided any other information
        so we should see a non-zero status returned as an error.
        """
        assert bind_pids.main(self.job, None, None, True) == 1

    @pytest.mark.django_db
    def test_bind_pids(self, django_db_setup, django_db_blocker):
        """Do something."""
        uuid_ = "4060ee97-9c3f-4822-afaf-ebdf838284c3"
        # main(job, sip_uuid, shared_path, bind_pids_switch):
        print(x)
        with django_db_blocker.unblock():
            for fixture in self.fixtures:
                call_command('loaddata', fixture)
        bind_pids.main(self.job, uuid_, "", True)
        assert False



