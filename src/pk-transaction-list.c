/* -*- Mode: C; tab-width: 8; indent-tabs-mode: t; c-basic-offset: 8 -*-
 *
 * Copyright (C) 2007 Richard Hughes <richard@hughsie.com>
 *
 * Licensed under the GNU General Public License Version 2
 *
 * This program is free software; you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation; either version 2 of the License, or
 * (at your option) any later version.
 *
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.
 *
 * You should have received a copy of the GNU General Public License
 * along with this program; if not, write to the Free Software
 * Foundation, Inc., 59 Temple Place - Suite 330, Boston, MA 02111-1307, USA.
 */

#include "config.h"

#include <stdlib.h>
#include <stdio.h>
#include <time.h>
#include <errno.h>
#include <signal.h>

#include <string.h>
#include <sys/time.h>
#include <sys/types.h>
#ifdef HAVE_UNISTD_H
#include <unistd.h>
#endif /* HAVE_UNISTD_H */

#include <glib/gi18n.h>
#include <dbus/dbus-glib.h>
#include <dbus/dbus-glib-lowlevel.h>

#include <pk-common.h>

#include "egg-debug.h"
#include "egg-string.h"

#include "pk-transaction-id.h"
#include "pk-transaction-list.h"
#include "pk-interface-transaction.h"

static void     pk_transaction_list_class_init	(PkTransactionListClass *klass);
static void     pk_transaction_list_init	(PkTransactionList      *tlist);
static void     pk_transaction_list_finalize	(GObject        *object);

#define PK_TRANSACTION_LIST_GET_PRIVATE(o) (G_TYPE_INSTANCE_GET_PRIVATE ((o), PK_TYPE_TRANSACTION_LIST, PkTransactionListPrivate))

/* how long the transaction should be queriable after it is finished, in seconds */
#define PK_TRANSACTION_LIST_KEEP_FINISHED_TIMEOUT	5

struct PkTransactionListPrivate
{
	GPtrArray		*array;
};

typedef struct {
	gboolean		 committed;
	gboolean		 running;
	gboolean		 finished;
	PkTransaction		*transaction;
	gchar			*tid;
} PkTransactionItem;

enum {
	PK_TRANSACTION_LIST_CHANGED,
	PK_TRANSACTION_LIST_LAST_SIGNAL
};

static guint signals [PK_TRANSACTION_LIST_LAST_SIGNAL] = { 0 };

G_DEFINE_TYPE (PkTransactionList, pk_transaction_list, G_TYPE_OBJECT)
static gpointer pk_transaction_list_object = NULL;

/**
 * pk_transaction_list_get_from_tid:
 **/
static PkTransactionItem *
pk_transaction_list_get_from_tid (PkTransactionList *tlist, const gchar *tid)
{
	guint i;
	GPtrArray *array;
	PkTransactionItem *item;
	const gchar *tmptid;

	g_return_val_if_fail (tlist != NULL, NULL);
	g_return_val_if_fail (PK_IS_TRANSACTION_LIST (tlist), NULL);

	/* find the runner with the transaction ID */
	array = tlist->priv->array;
	for (i=0; i<array->len; i++) {
		item = (PkTransactionItem *) g_ptr_array_index (array, i);
		tmptid = pk_transaction_get_tid (item->transaction);
		if (egg_strequal (tmptid, tid))
			return item;
	}
	return NULL;
}

/**
 * pk_transaction_list_role_present:
 *
 * if there is a queued transaction with this role, useful to avoid having
 * multiple system updates queued
 **/
gboolean
pk_transaction_list_role_present (PkTransactionList *tlist, PkRoleEnum role)
{
	guint i;
	GPtrArray *array;
	PkRoleEnum role_temp;
	PkTransactionItem *item;

	g_return_val_if_fail (PK_IS_TRANSACTION_LIST (tlist), FALSE);

	/* check for existing transaction doing an update */
	array = tlist->priv->array;
	for (i=0; i<array->len; i++) {
		item = (PkTransactionItem *) g_ptr_array_index (array, i);
		/* we might have recently finished this, but not removed it */
		if (item->finished)
			continue;
		/* we might not have this set yet */
		if (item->transaction == NULL)
			continue;
		role_temp = pk_transaction_priv_get_role (item->transaction);
		if (role_temp == role)
			return TRUE;
	}
	return FALSE;
}

/**
 * pk_transaction_list_remove_internal:
 **/
gboolean
pk_transaction_list_remove_internal (PkTransactionList *tlist, PkTransactionItem *item)
{
	gboolean ret;

	g_return_val_if_fail (PK_IS_TRANSACTION_LIST (tlist), FALSE);
	g_return_val_if_fail (item != NULL, FALSE);

	/* valid item */
	egg_debug ("remove transaction %s, item %p", item->tid, item);
	ret = g_ptr_array_remove (tlist->priv->array, item);
	if (!ret) {
		egg_warning ("could not remove %p as not present in list", item);
		return FALSE;
	}
	g_object_unref (item->transaction);
	g_free (item->tid);
	g_free (item);

	return TRUE;
}

/**
 * pk_transaction_list_remove:
 **/
gboolean
pk_transaction_list_remove (PkTransactionList *tlist, const gchar *tid)
{
	PkTransactionItem *item;
	gboolean ret;

	g_return_val_if_fail (PK_IS_TRANSACTION_LIST (tlist), FALSE);
	g_return_val_if_fail (tid != NULL, FALSE);

	item = pk_transaction_list_get_from_tid (tlist, tid);
	if (item == NULL) {
		egg_warning ("could not get item");
		return FALSE;
	}
	if (item->finished) {
		egg_warning ("already finished, so waiting to timeout");
		return FALSE;
	}
	ret = pk_transaction_list_remove_internal (tlist, item);
	return ret;
}

/* we need this for the finished data */
typedef struct {
	PkTransactionList *tlist;
	PkTransactionItem *item;
} PkTransactionFinished;

/**
 * pk_transaction_list_remove_item_timeout:
 **/
static gboolean
pk_transaction_list_remove_item_timeout (gpointer data)
{
	PkTransactionFinished *finished = (PkTransactionFinished *) data;

	egg_debug ("transaction %s completed, removing", finished->item->tid);
	pk_transaction_list_remove_internal (finished->tlist, finished->item);
	g_free (finished);
	return FALSE;
}

/**
 * pk_transaction_list_transaction_finished_cb:
 **/
static void
pk_transaction_list_transaction_finished_cb (PkTransaction *transaction, const gchar *exit_text, guint time, PkTransactionList *tlist)
{
	guint i;
	guint length;
	gboolean ret;
	PkTransactionItem *item;
	PkTransactionFinished *finished;
	const gchar *tid;

	g_return_if_fail (PK_IS_TRANSACTION_LIST (tlist));

	tid = pk_transaction_get_tid (transaction);
	item = pk_transaction_list_get_from_tid (tlist, tid);
	if (item == NULL)
		egg_error ("no transaction list item found!");

	/* transaction is already finished? */
	if (item->finished) {
		egg_warning ("transaction %s finished twice!", item->tid);
		return;
	}

	egg_debug ("transaction %s completed, marking finished", item->tid);
	item->finished = TRUE;

	/* we have changed what is running */
	egg_debug ("emmitting ::changed");
	g_signal_emit (tlist, signals [PK_TRANSACTION_LIST_CHANGED], 0);

	/* give the client a few seconds to still query the runner */
	finished = g_new0 (PkTransactionFinished, 1);
	finished->tlist = tlist;
	finished->item = item;
	g_timeout_add_seconds (PK_TRANSACTION_LIST_KEEP_FINISHED_TIMEOUT, pk_transaction_list_remove_item_timeout, finished);

	/* do the next transaction now if we have another queued */
	length = tlist->priv->array->len;
	for (i=0; i<length; i++) {
		item = (PkTransactionItem *) g_ptr_array_index (tlist->priv->array, i);
		if (item->committed &&
		    !item->running &&
		    !item->finished) {
			egg_debug ("running %s", item->tid);
			item->running = TRUE;
			ret = pk_transaction_run (item->transaction);
			/* only stop lookng if we run the job */
			if (ret)
				break;
		}
	}
}

/**
 * pk_transaction_list_create:
 **/
gboolean
pk_transaction_list_create (PkTransactionList *tlist, const gchar *tid)
{
	gboolean ret;
	PkTransactionItem *item;
	DBusGConnection *connection;

	g_return_val_if_fail (PK_IS_TRANSACTION_LIST (tlist), FALSE);
	g_return_val_if_fail (tid != NULL, FALSE);

	/* already added? */
	item = pk_transaction_list_get_from_tid (tlist, tid);
	if (item != NULL) {
		egg_warning ("already added %s to list", tid);
		return FALSE;
	}

	/* add to the array */
	item = g_new0 (PkTransactionItem, 1);
	item->committed = FALSE;
	item->running = FALSE;
	item->finished = FALSE;
	item->transaction = NULL;
	item->tid = g_strdup (tid);

	/* get another connection */
	connection = dbus_g_bus_get (DBUS_BUS_SYSTEM, NULL);
	if (connection == NULL)
		egg_error ("no connection");

	item->transaction = pk_transaction_new ();
	g_signal_connect_after (item->transaction, "finished",
				G_CALLBACK (pk_transaction_list_transaction_finished_cb), tlist);

	/* set the TID on the transaction */
	ret = pk_transaction_set_tid (item->transaction, item->tid);
	if (!ret)
		egg_error ("failed to set TID");

	/* put on the bus */
	dbus_g_object_type_install_info (PK_TYPE_TRANSACTION, &dbus_glib_pk_transaction_object_info);
	dbus_g_connection_register_g_object (connection, item->tid, G_OBJECT (item->transaction));

	egg_debug ("adding transaction %p, item %p", item->transaction, item);
	g_ptr_array_add (tlist->priv->array, item);
	return TRUE;
}

/**
 * pk_transaction_list_number_running:
 **/
static guint
pk_transaction_list_number_running (PkTransactionList *tlist)
{
	guint i;
	guint count = 0;
	guint length;
	PkTransactionItem *item;

	g_return_val_if_fail (PK_IS_TRANSACTION_LIST (tlist), 0);

	/* find all the transactions in progress */
	length = tlist->priv->array->len;
	for (i=0; i<length; i++) {
		item = (PkTransactionItem *) g_ptr_array_index (tlist->priv->array, i);
		if (item->committed && item->running && !item->finished)
			count++;
	}
	return count;
}

/**
 * pk_transaction_list_commit:
 **/
gboolean
pk_transaction_list_commit (PkTransactionList *tlist, const gchar *tid)
{
	gboolean ret;
	PkTransactionItem *item;

	g_return_val_if_fail (PK_IS_TRANSACTION_LIST (tlist), FALSE);
	g_return_val_if_fail (tid != NULL, FALSE);

	item = pk_transaction_list_get_from_tid (tlist, tid);
	if (item == NULL) {
		egg_warning ("could not get transaction: %s", tid);
		return FALSE;
	}

	egg_debug ("marking transaction %s as committed", item->tid);
	item->committed = TRUE;

	/* we will changed what is running */
	egg_debug ("emitting ::changed");
	g_signal_emit (tlist, signals [PK_TRANSACTION_LIST_CHANGED], 0);

	/* do the transaction now if we have no other in progress */
	if (pk_transaction_list_number_running (tlist) == 0) {
		egg_debug ("running %s", item->tid);
		item->running = TRUE;
		ret = pk_transaction_run (item->transaction);
		if (!ret) {
			egg_warning ("unable to start first job");
			return FALSE;
		}
	}

	return TRUE;
}

/**
 * pk_transaction_list_get_array:
 **/
gchar **
pk_transaction_list_get_array (PkTransactionList *tlist)
{
	guint i;
	guint length;
	GPtrArray *parray;
	gchar **array;
	PkTransactionItem *item;

	g_return_val_if_fail (PK_IS_TRANSACTION_LIST (tlist), NULL);

	/* use a temp array, as not all are in progress */
	parray = g_ptr_array_new ();

	/* find all the transactions in progress */
	length = tlist->priv->array->len;
	for (i=0; i<length; i++) {
		item = (PkTransactionItem *) g_ptr_array_index (tlist->priv->array, i);
		/* only return in the list if its committed and not finished */
		if (item->committed && !item->finished)
			g_ptr_array_add (parray, g_strdup (item->tid));
	}
	egg_debug ("%i transactions in list, %i active", length, parray->len);
	array = pk_ptr_array_to_argv (parray);
	g_ptr_array_free (parray, TRUE);

	return array;
}

/**
 * pk_transaction_list_get_size:
 **/
guint
pk_transaction_list_get_size (PkTransactionList *tlist)
{
	g_return_val_if_fail (PK_IS_TRANSACTION_LIST (tlist), 0);
	return tlist->priv->array->len;
}

/**
 * pk_transaction_list_class_init:
 * @klass: The PkTransactionListClass
 **/
static void
pk_transaction_list_class_init (PkTransactionListClass *klass)
{
	GObjectClass *object_class = G_OBJECT_CLASS (klass);

	object_class->finalize = pk_transaction_list_finalize;

	signals [PK_TRANSACTION_LIST_CHANGED] =
		g_signal_new ("changed",
			      G_TYPE_FROM_CLASS (object_class), G_SIGNAL_RUN_LAST,
			      0, NULL, NULL, g_cclosure_marshal_VOID__VOID,
			      G_TYPE_NONE, 0);

	g_type_class_add_private (klass, sizeof (PkTransactionListPrivate));
}

/**
 * pk_transaction_list_init:
 * @tlist: This class instance
 **/
static void
pk_transaction_list_init (PkTransactionList *tlist)
{
	tlist->priv = PK_TRANSACTION_LIST_GET_PRIVATE (tlist);
	tlist->priv->array = g_ptr_array_new ();
}

/**
 * pk_transaction_list_finalize:
 * @object: The object to finalize
 **/
static void
pk_transaction_list_finalize (GObject *object)
{
	PkTransactionList *tlist;

	g_return_if_fail (PK_IS_TRANSACTION_LIST (object));

	tlist = PK_TRANSACTION_LIST (object);

	g_return_if_fail (tlist->priv != NULL);

	g_ptr_array_free (tlist->priv->array, TRUE);

	G_OBJECT_CLASS (pk_transaction_list_parent_class)->finalize (object);
}

/**
 * pk_transaction_list_new:
 *
 * Return value: a new PkTransactionList object.
 **/
PkTransactionList *
pk_transaction_list_new (void)
{
	if (pk_transaction_list_object != NULL) {
		g_object_ref (pk_transaction_list_object);
	} else {
		pk_transaction_list_object = g_object_new (PK_TYPE_TRANSACTION_LIST, NULL);
		g_object_add_weak_pointer (pk_transaction_list_object, &pk_transaction_list_object);
	}
	return PK_TRANSACTION_LIST (pk_transaction_list_object);
}

/***************************************************************************
 ***                          MAKE CHECK TESTS                           ***
 ***************************************************************************/
#ifdef EGG_TEST
#include "egg-test.h"
#include "pk-backend-internal.h"

/**
 * pk_transaction_list_test_finished_cb:
 **/
static void
pk_transaction_list_test_finished_cb (PkTransaction *transaction, const gchar *exit_text, guint time, EggTest *test)
{
	egg_test_loop_quit (test);
}

/**
 * pk_transaction_list_test_delay_cb:
 **/
static void
pk_transaction_list_test_delay_cb (EggTest *test)
{
	egg_test_loop_quit (test);
}

void
pk_transaction_list_test (EggTest *test)
{
	PkTransactionList *tlist;
	gboolean ret;
	gchar *tid;
	guint size;
	gchar **array;
	PkTransactionItem *item;

	if (!egg_test_start (test, "PkTransactionList"))
		return;

	/************************************************************/
	egg_test_title (test, "get a transaction list object");
	tlist = pk_transaction_list_new ();
	if (tlist != NULL)
		egg_test_success (test, NULL);
	else
		egg_test_failed (test, NULL);

	/************************************************************/
	egg_test_title (test, "make sure we get a valid tid");
	tid = pk_transaction_id_generate ();
	if (tid != NULL) {
		egg_test_success (test, "got tid %s", tid);
	} else
		egg_test_failed (test, "failed to get tid");

	/************************************************************/
	egg_test_title (test, "create a transaction object");
	ret = pk_transaction_list_create (tlist, tid);
	if (ret) {
		egg_test_success (test, "created transaction %s", tid);
	} else
		egg_test_failed (test, "failed to create transaction");

	/************************************************************/
	egg_test_title (test, "make sure we get the right object back");
	item = pk_transaction_list_get_from_tid (tlist, tid);
	if (item != NULL &&
	    egg_strequal (item->tid, tid) &&
	    item->transaction != NULL)
		egg_test_success (test, NULL);
	else
		egg_test_failed (test, "could not find in db");

	/************************************************************/
	egg_test_title (test, "get size one we have in queue");
	size = pk_transaction_list_get_size (tlist);
	if (size == 1)
		egg_test_success (test, NULL);
	else
		egg_test_failed (test, "size %i", size);

	/************************************************************/
	egg_test_title (test, "get transactions in progress");
	array = pk_transaction_list_get_array (tlist);
	size = g_strv_length (array);
	if (size == 0)
		egg_test_success (test, NULL);
	else
		egg_test_failed (test, "size %i", size);

	/************************************************************/
	egg_test_title (test, "add again the same tid (should fail)");
	ret = pk_transaction_list_create (tlist, tid);
	if (!ret)
		egg_test_success (test, NULL);
	else
		egg_test_failed (test, "added the same tid twice");

	/************************************************************/
	egg_test_title (test, "remove without ever committing");
	ret = pk_transaction_list_remove (tlist, tid);
	if (ret)
		egg_test_success (test, NULL);
	else
		egg_test_failed (test, "failed to remove");

	/************************************************************/
	egg_test_title (test, "get size none we have in queue");
	size = pk_transaction_list_get_size (tlist);
	if (size == 0)
		egg_test_success (test, NULL);
	else
		egg_test_failed (test, "size %i", size);

	/* get a new tid */
	g_free (tid);
	tid = pk_transaction_id_generate ();

	/************************************************************/
	egg_test_title (test, "create another item");
	ret = pk_transaction_list_create (tlist, tid);
	if (ret)
		egg_test_success (test, "created transaction %s", tid);
	else
		egg_test_failed (test, "failed to create transaction");

	/************************************************************/
	PkBackend *backend;
	backend = pk_backend_new ();
	egg_test_title (test, "try to load a valid backend");
	ret = pk_backend_set_name (backend, "dummy");
	if (ret)
		egg_test_success (test, NULL);
	else
		egg_test_failed (test, NULL);

	/************************************************************/
	egg_test_title (test, "lock an valid backend");
	ret = pk_backend_lock (backend);
	if (ret)
		egg_test_success (test, NULL);
	else
		egg_test_failed (test, "failed to lock");

	/************************************************************/
	egg_test_title (test, "get from db");
	item = pk_transaction_list_get_from_tid (tlist, tid);
	if (item != NULL &&
	    egg_strequal (item->tid, tid) &&
	    item->transaction != NULL)
		egg_test_success (test, NULL);
	else
		egg_test_failed (test, "could not find in db");

	g_signal_connect (item->transaction, "finished",
				G_CALLBACK (pk_transaction_list_test_finished_cb), test);

	pk_transaction_get_updates (item->transaction, "none", NULL);

	/************************************************************/
	egg_test_title (test, "get present role");
	ret = pk_transaction_list_role_present (tlist, PK_ROLE_ENUM_GET_UPDATES);
	if (ret)
		egg_test_success (test, NULL);
	else
		egg_test_failed (test, "did not get role");

	/************************************************************/
	egg_test_title (test, "get non-present role");
	ret = pk_transaction_list_role_present (tlist, PK_ROLE_ENUM_SEARCH_NAME);
	if (!ret)
		egg_test_success (test, NULL);
	else
		egg_test_failed (test, "got missing role");

	/************************************************************/
	egg_test_title (test, "get size one we have in queue");
	size = pk_transaction_list_get_size (tlist);
	if (size == 1)
		egg_test_success (test, NULL);
	else
		egg_test_failed (test, "size %i", size);

	/************************************************************/
	egg_test_title (test, "get transactions in progress");
	array = pk_transaction_list_get_array (tlist);
	size = g_strv_length (array);
	if (size == 1)
		egg_test_success (test, NULL);
	else
		egg_test_failed (test, "size %i", size);

	/* wait for Finished */
	egg_test_loop_wait (test, 2000);
	egg_test_loop_check (test);

	/************************************************************/
	egg_test_title (test, "get size one we have in queue");
	size = pk_transaction_list_get_size (tlist);
	if (size == 1)
		egg_test_success (test, NULL);
	else
		egg_test_failed (test, "size %i", size);

	/************************************************************/
	egg_test_title (test, "get transactions in progress (none)");
	array = pk_transaction_list_get_array (tlist);
	size = g_strv_length (array);
	if (size == 0)
		egg_test_success (test, NULL);
	else
		egg_test_failed (test, "size %i", size);

	/************************************************************/
	egg_test_title (test, "remove already removed");
	ret = pk_transaction_list_remove (tlist, tid);
	if (!ret)
		egg_test_success (test, NULL);
	else
		egg_test_failed (test, "tried to remove");

	/* wait for Cleanup */
	g_timeout_add_seconds (5, (GSourceFunc) pk_transaction_list_test_delay_cb, test);
	egg_test_loop_wait (test, 6000);
	egg_test_loop_check (test);

	/************************************************************/
	egg_test_title (test, "make sure queue empty");
	size = pk_transaction_list_get_size (tlist);
	if (size == 0)
		egg_test_success (test, NULL);
	else
		egg_test_failed (test, "size %i", size);

	g_free (tid);

	g_object_unref (tlist);
	g_object_unref (backend);

	egg_test_end (test);
}
#endif

