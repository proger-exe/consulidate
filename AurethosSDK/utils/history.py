import logging
import time
from typing import Dict, List, Optional, Union


class HistoryStorage:
    """
    A class to store and manage chat history with different roles (assistant, system, user).
    Supports adding, viewing, deleting, and retrieving all history entries.
    """

    def __init__(self):
        """
        Initialize the history storage with an empty list.
        """
        self.history: List[Dict[str, Union[str, dict]]] = []
        self.logger = logging.getLogger(self.__class__.__name__)

    def add_entry(
        self,
        role: str,
        content: Union[str, dict],
        timestamp: Optional[int] = None,
        **kwargs,
    ) -> Dict[str, Union[str, dict]]:
        """
        Add a new entry to the history.

        Args:
            role(str): The role of the message sender ('assistant', 'system', 'user')
            content(Union[str, dict]): The content of the message (string or dictionary)
            timestamp(Optional[int]): 13-digit timestamp string. If None, current time will be used.

        Returns:
            The added history entry

        Raises:
            ValueError: If role is not one of the allowed values
        """
        if role not in ["assistant", "system", "user"]:
            raise ValueError("Role must be 'assistant', 'system', or 'user'")

        if timestamp is None:
            timestamp = str(int(time.time() * 1000))

        entry = {
            "role": role,
            "content": content,
        }

        for k, v in kwargs.items():
            entry[k] = v

        if role in ["assistant", "user"]:
            entry["timestamp"] = str(timestamp)  # он же timestamp

        self.history.append(entry)
        self.logger.debug(f"History recorded: {entry}")
        return entry

    def import_entires(self, entries: List[Dict[str, Union[str, dict]]]) -> None:
        """
        Mass import to history, changes current history entries on `entries` list.

        Args:
            entries (List[Dict[str, Union[str, dict]]]): _description_
        """
        self.history = entries
        self.logger.debug("Imported %s entries to model history.", len(entries))

    def get_all_entries(
        self, clear_all: bool = False
    ) -> List[Dict[str, Union[str, dict]]]:
        """
        Get all history entries.

        Returns:
            A list of all history entries
        """
        history_copy = self.history.copy()
        if clear_all:
            self.clear_history()

        return history_copy

    def get_entries_by_role(self, role: str) -> List[Dict[str, Union[str, dict]]]:
        """
        Get all history entries for a specific role.

        Args:
            role: The role to filter by ('assistant', 'system', 'user')

        Returns:
            A list of history entries with the specified role

        Raises:
            ValueError: If role is not one of the allowed values
        """
        if role not in ["assistant", "system", "user"]:
            raise ValueError("Role must be 'assistant', 'system', or 'user'")

        return [entry for entry in self.history if entry["role"] == role]

    def delete_by_role(self, role: str, count: Optional[int] = None) -> int:
        """
        Delete history entries with the specified role, optionally limited by count.

        Args:
            role: The role to filter by ('assistant', 'system', or 'user')
            count: Maximum number of entries to delete (None means delete all)

        Returns:
            Number of entries actually deleted

        Raises:
            ValueError: If role is invalid or count is negative
        """
        if role not in ["assistant", "system", "user"]:
            raise ValueError("Role must be 'assistant', 'system', or 'user'")
        if count is not None and count < 0:
            raise ValueError("Count cannot be negative")

        deleted_count = 0

        # Iterate backwards to safely delete while iterating
        for i in range(len(self.history) - 1, -1, -1):
            if self.history[i]["role"] == role:
                del self.history[i]
                deleted_count += 1
                if count is not None and deleted_count >= count:
                    break

        self.logger.debug(f"Deleted {deleted_count} items by role {role} from history")

        return deleted_count

    def delete_all_non_system_entries(self) -> int:
        """
        Delete all history entries except those with role='system'.

        Returns:
            Number of entries deleted
        """
        initial_count = len(self.history)

        # Iterate backwards to safely delete while iterating
        for i in range(len(self.history) - 1, -1, -1):
            if self.history[i]["role"] != "system":
                del self.history[i]

        self.logger.debug(
            f"Deleted {initial_count - len(self.history)} items that non match role='system' from history"
        )

        return initial_count - len(self.history)

    def get_entry_by_timestamp(
        self, timestamp: str
    ) -> Optional[Dict[str, Union[str, dict]]]:
        """
        Get a history entry by its timestamp.

        Args:
            timestamp: The 13-digit timestamp string to search for

        Returns:
            The matching history entry, or None if not found
        """
        for entry in self.history:
            if entry["timestamp"] == timestamp:
                return entry
        return None

    def delete_entry_by_timestamp(self, timestamp: str) -> bool:
        """
        Delete a history entry by its timestamp.

        Args:
            timestamp: The 13-digit timestamp string of the entry to delete

        Returns:
            True if an entry was deleted, False if no matching entry was found
        """
        for i, entry in enumerate(self.history):
            if entry["timestamp"] == timestamp:
                del self.history[i]
                return True
        return False

    def clear_history(self) -> None:
        """Clear all history entries."""
        self.history = []
        self.logger.debug("History cleared")

    def get_last_entry(self) -> Optional[Dict[str, Union[str, dict]]]:
        """
        Get the most recent history entry.

        Returns:
            The most recent entry, or None if history is empty
        """
        if not self.history:
            return None
        return self.history[-1]

    def get_entries_since(self, timestamp: str) -> List[Dict[str, Union[str, dict]]]:
        """
        Get all entries since a specific timestamp.

        Args:
            timestamp: The 13-digit timestamp string to compare against

        Returns:
            A list of entries with timestamps greater than the specified value
        """
        return [entry for entry in self.history if entry["timestamp"] > timestamp]

    def delete_entries_since(self, timestamp: str) -> bool:
        """
        Delete all history entries with timestamps greater than or equal to the specified timestamp.

        Args:
            timestamp: The 13-digit timestamp string (entries with this timestamp or newer will be deleted)

        Returns:
            True if any entries were deleted, False if no deletions occurred
        """
        original_length = len(self.history)

        # Iterate backwards to safely delete while iterating
        for i in range(len(self.history) - 1, -1, -1):
            if self.history[i]["timestamp"] >= timestamp:
                del self.history[i]

        self.logger.debug(
            f"Deleted {original_length - len(self.history)} items by timestamp {timestamp} from history"
        )

        return len(self.history) < original_length

    def delete_last_by_count(self, count: int) -> bool:
        """
        Delete the last N entries from the history.

        Args:
            count: Number of most recent entries to delete

        Returns:
            True if entries were deleted, False if count was 0 or history was empty

        Raises:
            ValueError: If count is negative
        """
        if count < 0:
            raise ValueError("Count cannot be negative")

        if count == 0 or not self.history:
            return False

        # Ensure we don't try to delete more entries than exist
        actual_delete_count = min(count, len(self.history))

        # Delete the last N entries
        del self.history[-actual_delete_count:]

        self.logger.debug(f"Deleted {actual_delete_count} items by count from history")
        return True
