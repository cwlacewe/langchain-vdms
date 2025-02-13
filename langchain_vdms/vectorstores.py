"""This is the langchain_vdms.vectorstores module.

It contains the VDMS class which is a vector store for handling various tasks.
"""

from __future__ import annotations

import base64
import logging
import time
import uuid
from copy import deepcopy
from os import path as os_path
from typing import (
    Any,
    Callable,
    Iterable,
    List,
    Literal,  # Added
    Optional,
    Sequence,
    Sized,
    Tuple,
    Type,
    TypeVar,
    Union,  # Added
    get_args,  # Added
)

import numpy as np
import vdms
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_core.vectorstores import VectorStore

from langchain_vdms.utils import maximal_marginal_relevance

VST = TypeVar("VST", bound=VectorStore)

logger = logging.getLogger(__name__)


# Helpful Variables
TEXT_PROPERTY = "content"  # Property name for the text
DEFAULT_COLLECTION_NAME = "langchain"
LANGCHAIN_ID_PROPERTY = "langchain_id"  # Property name for the unique id
DEFAULT_INSERT_BATCH_SIZE = 500
DEFAULT_K = 3  # Number of Documents to return.
DEFAULT_FETCH_K = (
    DEFAULT_K * 5
)  # Number of Documents to fetch to pass to knn when filters applied.
INVALID_METADATA_VALUE = ["Missing property", None, {}]  # type: list
DEFAULT_PROPERTIES = ["_distance", LANGCHAIN_ID_PROPERTY, TEXT_PROPERTY]
INVALID_DOC_METADATA_KEYS = ["_distance", TEXT_PROPERTY, "blob"]

DISTANCE_METRICS = Literal[
    "L2",  # Euclidean Distance
    "IP",  # Inner Product
]
AVAILABLE_DISTANCE_METRICS: list[DISTANCE_METRICS] = list(get_args(DISTANCE_METRICS))

ENGINES = Literal[
    "FaissFlat",  # FAISS IndexFlat
    "FaissHNSWFlat",  # FAISS IndexHNSWFlat
    "FaissIVFFlat",  # FAISS IndexIVFFlat
    "Flinng",  # FLINNG
    "TileDBDense",  # TileDB Dense
    "TileDBSparse",  # TileDB Sparse
]
AVAILABLE_ENGINES: list[ENGINES] = list(get_args(ENGINES))


# Vectorstore Implementation
class VDMS(VectorStore):
    """Intel Lab's VDMS vector store integration.

    A single VDMS instance can support numerous vector stores and use
    different distance metrics and engines. The vector stores must have a
    unique collection_name (DescriptorSet).

    Visit https://github.com/IntelLabs/vdms/wiki for more information.

    IT IS HIGHLY SUGGESTED TO NORMALIZE YOUR DATA.

    Setup:
        Install ``langchain-vdms`` and deploy VDMS server.
        For simplicity, below will use the VDMS docker image.
        Keep note of the host (``VDMS_HOST``) and port (``VDMS_PORT``) associated
        with a deployed VDMS Server.

        .. code-block:: bash

            pip install -U langchain-vdms
            docker run -d -p ``VDMS_PORT``:55555 intellabs/vdms:latest

    Key init args — client params:
        client: vdms.vdms
            VDMS Client used to connect to VDMS server.

    Key init args — indexing params:
        collection_name: str
            Name of the collection.
        embedding: Embeddings
            Embedding function to use.
        embedding_dimensions: int
            Dimensions of embedding function.
        engine: ENGINES
            Underlying implementation for indexing
            VDMS supports TileDBDense, TileDBSparse, FaissFlat, FaissIVFFlat,
                FaissHNSWFlat, and Flinng [Default: FaissFlat]
        distance_strategy: DISTANCE_METRICS
            Distance metric to use.
            VDMS supports"L2" (euclidean distance) or "IP" (inner product) [Default: L2]

    Instantiate:
        .. code-block:: python

            from langchain_vdms.vectorstores import VDMS, VDMS_Client
            from langchain_openai import OpenAIEmbeddings
            embedding = OpenAIEmbeddings()
            from langchain_huggingface import HuggingFaceEmbeddings
            model_name = "sentence-transformers/all-mpnet-base-v2"
            embedding = HuggingFaceEmbeddings(model_name=model_name)

            vector_store = VDMS(
                client=VDMS_Client(VDMS_HOST, VDMS_PORT),
                collection_name="foo",
                embedding=embedding,
                engine="FaissFlat",
                distance_strategy="L2",
            )

    Add Documents:
        .. code-block:: python

            from langchain_core.documents import Document

            document_1 = Document(page_content="foo", metadata={"baz": "bar"})
            document_2 = Document(page_content="thud", metadata={"bar": "baz"})
            document_3 = Document(page_content="i will be deleted :(")

            documents = [document_1, document_2, document_3]
            ids = ["1", "2", "3"]
            vector_store.add_documents(documents=documents, ids=ids)

    Update Documents:
        .. code-block:: python
            updated_document = Document(
                page_content="qux",
                metadata={"source": "https://another-example.com"}
            )

            vector_store.update_documents(ids=["1"],documents=[updated_document])
            # vector_store.update_document(document_id="1",document=updated_document)

    Delete Documents:
        .. code-block:: python

            vector_store.delete(ids=["3"])

    Search:
        .. code-block:: python

            results = vector_store.similarity_search(query="thud",k=1)
            for doc in results:
                print(f"* {doc.page_content} [{doc.metadata}]")

        .. code-block:: python

            * thud [{'bar': 'baz'}]

    Search with filter:
        .. code-block:: python

            results = vector_store.similarity_search(query="thud",k=1,filter={"bar": ["==", "baz"]})
            for doc in results:
                print(f"* {doc.page_content} [{doc.metadata}]")

        .. code-block:: python

            * thud [{'bar': 'baz'}]

    Search with score:
        .. code-block:: python

            results = vector_store.similarity_search_with_score(query="qux",k=1)
            for doc, score in results:
                print(f"* [SIM={score:3f}] {doc.page_content} [{doc.metadata}]")

        .. code-block:: python

            * [SIM=1.496966] foo [{'baz': 'bar', 'langchain_id': '1'}]

    Use as Retriever:
        .. code-block:: python

            retriever = vector_store.as_retriever(
                search_type="mmr",
                search_kwargs={"k": 1, "fetch_k": 2, "lambda_mult": 0.5},
            )
            retriever.invoke("thud")

        .. code-block:: python

            [Document(metadata={'bar': 'baz', 'langchain_id': '2'}, page_content='thud')]

    """  # noqa: E501

    _database: dict[str, tuple[Document, list[float]]] = {}

    def __init__(
        self,
        client: vdms.vdms,
        *,
        embedding: Embeddings,
        embedding_dimensions: Optional[int] = None,
        collection_name: str = DEFAULT_COLLECTION_NAME,
        engine: ENGINES = "FaissFlat",
        distance_strategy: DISTANCE_METRICS = "L2",
        relevance_score_fn: Optional[Callable[[float], float]] = None,
        **kwargs: Any,
    ) -> None:
        self.collection_name = collection_name

        # Check required parameters
        self._client = client
        self.utils = VDMS_Utils(client)
        self.similarity_search_engine = engine
        self.distance_strategy = distance_strategy
        self.embedding = embedding
        self._check_required_inputs(collection_name, embedding_dimensions, **kwargs)
        self.updated_properties_flag = False
        self.override_relevance_score_fn = relevance_score_fn

        # Initialize collection
        self._create_collection()

    def add_texts(
        self,
        texts: Iterable[str],
        metadatas: Optional[List[dict]] = None,
        **kwargs: Any,
    ) -> List[str]:
        # raise NotImplementedError
        """Run texts through the embeddings function and add to the vector store.

        Args:
            texts: Iterable of strings to add to the vector store.
            metadatas: Optional list of metadatas associated with the texts.
            ids: Optional list of IDs associated with the texts.
            **kwargs: vector store specific parameters.
                One of the kwargs should be `ids` which is a list of ids
                associated with the texts.

        Returns:
            List of ids from adding the texts into the vector store.

        Raises:
            ValueError: If the number of metadatas does not match the number of texts.
            ValueError: If the number of ids does not match the number of texts.
        """

        texts_ = list(texts)

        embeddings = self.embed_documents(texts_)

        return self.add_embeddings(
            texts=texts_,
            embeddings=embeddings,
            metadatas=metadatas,
            **kwargs,
        )

    # optional: add custom async implementations
    # async def aadd_texts(
    #     self,
    #     texts: Iterable[str],
    #     metadatas: Optional[List[dict]] = None,
    #     **kwargs: Any,
    # ) -> List[str]:
    #     return await asyncio.get_running_loop().run_in_executor(
    #         None, partial(self.add_texts, **kwargs), texts, metadatas
    #     )

    def delete(self, ids: Optional[List[str]] = None, **kwargs: Any) -> Optional[bool]:
        # raise NotImplementedError
        """Delete by vector ID or other criteria.

        Args:
            ids: List of ids to delete. If None, delete all. Default is None.
            **kwargs: Other keyword arguments that subclasses might use.

        Returns:
            Optional[bool]: True if deletion is successful, False otherwise
        """

        collection_name = kwargs.pop("collection_name", self.collection_name)

        if "constraints" in kwargs and isinstance(kwargs["constraints"], dict):
            constraints = kwargs.pop("constraints")
            constraints["_deletion"] = ["==", 1]
        else:
            constraints = {"_deletion": ["==", 1]}

        response = self.batch_delete(
            collection_name,
            constraints,
            ids,
            batch_size=kwargs.get("batch_size", DEFAULT_INSERT_BATCH_SIZE),
        )

        # Update/store indices after deletion
        query = self.utils.add_descriptor_set(
            "FindDescriptorSet", collection_name, storeIndex=True
        )
        _, _ = self.utils.run_vdms_query([query])

        return "FindDescriptor" in response[0]

    # optional: add custom async implementations
    # async def adelete(
    #     self, ids: Optional[List[str]] = None, **kwargs: Any
    # ) -> Optional[bool]:
    #     raise NotImplementedError

    def similarity_search(
        self,
        query: str,
        k: int = DEFAULT_K,
        fetch_k: int = DEFAULT_FETCH_K,
        filter: Optional[dict[str, List]] = None,
        **kwargs: Any,
    ) -> List[Document]:
        # raise NotImplementedError
        """Return docs most similar to query.

        Args:
            query: Query string to search for.
            k: Number of Documents to return.
            fetch_k: Number of candidates to fetch for knn (>= k).
            filter: Filter by metadata. Defaults to None.
            **kwargs: Arguments to pass to the search method.

        Returns:
            List of Documents most similar to the query.
        """
        assert self.embedding is not None, "Embedding function is not set"
        query_embedding = self.get_embedding_from_query(query)
        return self.similarity_search_by_vector(
            query_embedding, k, fetch_k=fetch_k, filter=filter, **kwargs
        )

    # optional: add custom async implementations
    # async def asimilarity_search(
    #     self, query: str, k: int = 4, **kwargs: Any
    # ) -> List[Document]:
    #     # This is a temporary workaround to make the similarity search
    #     # asynchronous. The proper solution is to make the similarity search
    #     # asynchronous in the vector store implementations.
    #     func = partial(self.similarity_search, query, k=k, **kwargs)
    #     return await asyncio.get_event_loop().run_in_executor(None, func)

    def similarity_search_with_score(
        # self, *args: Any, **kwargs: Any
        self,
        query: str,
        k: int = DEFAULT_K,
        fetch_k: int = DEFAULT_FETCH_K,
        filter: Optional[dict[str, List]] = None,
        **kwargs: Any,
    ) -> List[Tuple[Document, float]]:
        # raise NotImplementedError
        query_embedding = self.get_embedding_from_query(query)
        results = self.query_by_embeddings(
            query_embeddings=[query_embedding],
            k=k,
            fetch_k=fetch_k,
            filter=filter,
            **kwargs,
        )

        return self.results2documents_and_scores(results)

    # optional: add custom async implementations
    # async def asimilarity_search_with_score(
    #     self, *args: Any, **kwargs: Any
    # ) -> List[Tuple[Document, float]]:
    #     # This is a temporary workaround to make the similarity search
    #     # asynchronous. The proper solution is to make the similarity search
    #     # asynchronous in the vector store implementations.
    #     func = partial(self.similarity_search_with_score, *args, **kwargs)
    #     return await asyncio.get_event_loop().run_in_executor(None, func)

    def similarity_search_by_vector(
        # self, embedding: List[float], k: int = DEFAULT_K, **kwargs: Any
        self,
        embedding: List[float],
        k: int = DEFAULT_K,
        fetch_k: int = DEFAULT_FETCH_K,
        filter: Optional[dict[str, List]] = None,
        **kwargs: Any,
    ) -> List[Document]:
        # raise NotImplementedError
        """Return docs most similar to embedding vector.

        Args:
            embedding: Embedding to look up documents similar to.
            k: Number of Documents to return.
            fetch_k: Number of candidates to fetch for knn (>= k).
            filter: Filter by metadata. Defaults to None.
            **kwargs: Arguments to pass to the search method.

        Returns:
            List of Documents most similar to the query vector.
        """
        start_time = time.time()
        results = self.query_by_embeddings(
            query_embeddings=[embedding],
            k=k,
            fetch_k=fetch_k,
            filter=filter,
            **kwargs,
        )
        logger.info(
            f"VDMS similarity search took {time.time() - start_time:0.4f} seconds"
        )

        final_docs = []
        for this_result in results:
            resp, _ = this_result
            try:
                descriptor_entities = resp[0]["FindDescriptor"].get("entities", [])
            except ValueError:
                descriptor_entities = []
            if isinstance(descriptor_entities, dict):
                final_docs.append(self.descriptor2document(descriptor_entities))
            elif isinstance(descriptor_entities, list):
                for descriptor in descriptor_entities:
                    final_docs.append(self.descriptor2document(descriptor))
            else:
                pass
        return final_docs

    # optional: add custom async implementations
    # async def asimilarity_search_by_vector(
    #     self, embedding: List[float], k: int = 4, **kwargs: Any
    # ) -> List[Document]:
    #     # This is a temporary workaround to make the similarity search
    #     # asynchronous. The proper solution is to make the similarity search
    #     # asynchronous in the vector store implementations.
    #     func = partial(self.similarity_search_by_vector, embedding, k=k, **kwargs)
    #     return await asyncio.get_event_loop().run_in_executor(None, func)

    def max_marginal_relevance_search(
        self,
        query: str,
        k: int = DEFAULT_K,
        fetch_k: int = DEFAULT_FETCH_K,
        lambda_mult: float = 0.5,
        filter: Optional[dict[str, List]] = None,
        **kwargs: Any,
    ) -> List[Document]:
        # raise NotImplementedError
        """Returns similar documents to the query that also have diversity

        This algorithm balances relevance and diversity in the search results.

        Args:
            query: Text to look up documents similar to.
            k: Number of Documents to return. Defaults to 4.
            fetch_k: Number of Documents to fetch to pass to MMR algorithm.
            lambda_mult: Number between 0 and 1 that determines the degree
                of diversity among the results with 0 corresponding
                to maximum diversity and 1 to minimum diversity.
                Defaults to 0.5.
            **kwargs: Arguments to pass to the search method.

        Returns:
            List of Document objects ordered by decreasing similarity/diversty.
        """
        query_embedding = self.get_embedding_from_query(query)
        return self.max_marginal_relevance_search_by_vector(
            query_embedding, k, fetch_k, lambda_mult, filter, **kwargs
        )

    # optional: add custom async implementations
    # async def amax_marginal_relevance_search(
    #     self,
    #     query: str,
    #     k: int = 4,
    #     fetch_k: int = 20,
    #     lambda_mult: float = 0.5,
    #     **kwargs: Any,
    # ) -> List[Document]:
    #     # This is a temporary workaround to make the similarity search
    #     # asynchronous. The proper solution is to make the similarity search
    #     # asynchronous in the vector store implementations.
    #     func = partial(
    #         self.max_marginal_relevance_search,
    #         query,
    #         k=k,
    #         fetch_k=fetch_k,
    #         lambda_mult=lambda_mult,
    #         **kwargs,
    #     )
    #     return await asyncio.get_event_loop().run_in_executor(None, func)

    def max_marginal_relevance_search_by_vector(
        self,
        embedding: List[float],
        k: int = DEFAULT_K,
        fetch_k: int = DEFAULT_FETCH_K,
        lambda_mult: float = 0.5,
        filter: Optional[dict[str, List]] = None,
        **kwargs: Any,
    ) -> List[Document]:
        # raise NotImplementedError
        """Return docs selected using the maximal marginal relevance.

        Maximal marginal relevance optimizes for similarity to query AND diversity
        among selected documents.

        Args:
            embedding: Embedding vector to search for.
            k: Number of Documents to return.
            fetch_k: Number of Documents to fetch to pass to MMR algorithm.
            lambda_mult: Number between 0 and 1 that determines the degree
                        of diversity among the results with 0 corresponding
                        to maximum diversity and 1 to minimum diversity.
                        Defaults to 0.5.
            filter (Optional[dict[str, str]]): Filter by metadata. Defaults to None.

        Returns:
            List of Documents selected by maximal marginal relevance.
        """
        start_time = time.time()
        results = self.query_by_embeddings(
            query_embeddings=[embedding],
            k=k,
            fetch_k=fetch_k,
            filter=filter,
            include=["metadatas", "documents", "distances", "embeddings"],
        )

        if len(results[0][1]) == 0:
            # No results returned
            return []
        else:
            embedding_list = [
                list(self.utils.bytes2embedding(result)) for result in results[0][1]
            ]

            mmr_selected = maximal_marginal_relevance(
                np.array(embedding, dtype=np.float32),
                embedding_list,
                k=k,
                lambda_mult=lambda_mult,
            )

            logger.info(
                f"VDMS similarity search mmr took {time.time() - start_time:0.4f} secs"
            )
            candidates = self.results2documents(results)
            return [r for i, r in enumerate(candidates) if i in mmr_selected]

    # optional: add custom async implementations
    # async def amax_marginal_relevance_search_by_vector(
    #     self,
    #     embedding: List[float],
    #     k: int = 4,
    #     fetch_k: int = 20,
    #     lambda_mult: float = 0.5,
    #     **kwargs: Any,
    # ) -> List[Document]:
    #     raise NotImplementedError

    @classmethod
    def from_texts(
        cls,
        texts: List[str],
        embedding: Embeddings,
        metadatas: Optional[List[dict]] = None,
        **kwargs: Any,
    ) -> VDMS:
        # raise NotImplementedError
        """Creates a new vector store from a list of texts

        Args:
            texts: List of text strings
            embedding: Embedding function to use.
            metadatas: Optional list of metadatas associated with the texts.
                Default is None.
            ids: Optional list of IDs associated with the texts.
            collection_name (str): Name of the collection to create.
            kwargs: Additional keyword arguments.
                - "delete_existing" will delete matching ids prior to adding
                    new document with same id (True); else add with duplicate
                    id (False) [Default: True]

        Returns:
            VectorStore: VectorStore initialized from texts and embeddings.
        """
        client: vdms.vdms = kwargs.pop("client")
        delete_existing: bool = kwargs.pop("delete_existing", True)
        collection_name = kwargs.get("collection_name", DEFAULT_COLLECTION_NAME)
        ids = kwargs.get("ids", None)

        if "batch_size" not in kwargs:
            kwargs["batch_size"] = DEFAULT_INSERT_BATCH_SIZE

        vdms_store = cls(
            client=client,
            embedding=embedding,
            collection_name=collection_name,
            **kwargs,
        )

        if ids is None:
            ids = [str(uuid.uuid4()) for _ in texts]

        metadatas = metadatas if metadatas is not None else [{} for _ in ids]
        vdms_store._len_check_if_sized(ids, texts, "ids", "texts")
        vdms_store._len_check_if_sized(ids, metadatas, "ids", "metadatas")

        # Remove IDs if exist
        remove_ids = [doc.id for doc in vdms_store.get_by_ids(ids) if doc.id]
        if len(remove_ids) > 0:
            if delete_existing:
                vdms_store.delete(ids=remove_ids, **kwargs)
                remove_ids = []
            else:
                pstr = "[!] Embeddings skipped for following ids because "
                pstr += f"already exists: {remove_ids}. Retry with "
                pstr += "'delete_existing' set to True"
                logger.info(pstr)

        valid_ids = []
        valid_texts = []
        valid_metadatas = []
        for id, txt, meta in zip(ids, texts, metadatas):
            if id not in remove_ids:
                valid_ids.append(id)
                valid_texts.append(txt)
                valid_metadatas.append(meta)

        vdms_store.add_texts(
            texts=valid_texts,
            metadatas=valid_metadatas,
            ids=valid_ids,
            **kwargs,
        )
        return vdms_store

    # optional: add custom async implementations
    # @classmethod
    # async def afrom_texts(
    #     cls: Type[VST],
    #     texts: List[str],
    #     embedding: Embeddings,
    #     metadatas: Optional[List[dict]] = None,
    #     **kwargs: Any,
    # ) -> VST:
    #     return await asyncio.get_running_loop().run_in_executor(
    #         None, partial(cls.from_texts, **kwargs), texts, embedding, metadatas
    #     )

    def _select_relevance_score_fn(self) -> Callable[[float], float]:
        # raise NotImplementedError
        """
        The 'correct' relevance function
        may differ depending on a few things, including:
        - the distance / similarity metric used by the VectorStore
        - the scale of your embeddings (OpenAI's are unit normed. Many others are not!)
        - embedding dimensionality
        - etc.
        """
        if self.override_relevance_score_fn is not None:
            return self.override_relevance_score_fn

        # Default strategy is to rely on distance strategy provided
        # in vector store constructor
        if self.distance_strategy.lower() in ["ip", "l2"]:
            return lambda x: x
        else:
            raise ValueError(
                "No supported normalization function"
                f" for distance_strategy of {self.distance_strategy}."
                "Consider providing relevance_score_fn to VDMS constructor."
            )

    def add_documents(self, documents: list[Document], **kwargs: Any) -> list[str]:
        """Add or update documents in the vector store.

        Args:
            documents: Documents to add to the vector store.
            kwargs: Additional keyword arguments.
                - if kwargs contains ids and documents contain ids,
                    the ids in the kwargs will receive precedence.
                - "delete_existing" will delete matching ids prior to adding
                    new document with same id (True); else add with duplicate
                    id (False) [Default: True]

        Returns:
            List of IDs of the added texts.

        Raises:
            ValueError: If the number of ids does not match the number of documents.
        """
        # GET IDS & FORMAT DOCUMENTS
        delete_existing: bool = kwargs.pop("delete_existing", True)
        ids = kwargs.pop("ids", None)
        if ids is not None:
            # Get IDs
            if ids and len(ids) != len(documents):
                raise ValueError(
                    "The number of ids must match the number of documents. "
                    "Got {len(ids)} ids and {len(documents)} documents."
                )

            # Get Documents
            documents_ = []
            for id_, document in zip(ids, documents):
                doc_with_id = Document(
                    page_content=document.page_content,
                    metadata=document.metadata,
                    id=id_,
                )
                documents_.append(doc_with_id)
        else:
            # Get Documents
            documents_ = documents

        if ids is None:
            ids = []
            for doc in documents_:
                if hasattr(doc, "id") and doc.id is not None:
                    ids.append(str(doc.id))
                elif "id" in doc.metadata:
                    ids.append(str(doc.metadata["id"]))
                else:
                    ids.append(str(uuid.uuid4()))

        # Remove IDs if exist
        remove_ids = [doc.id for doc in self.get_by_ids(ids) if doc.id]
        if len(remove_ids) > 0:
            if delete_existing:
                self.delete(ids=remove_ids, **kwargs)
                remove_ids = []
            else:
                pstr = "[!] Embeddings skipped for following ids because "
                pstr += f"already exists: {remove_ids}\nCan retry with "
                pstr += "'delete_existing' set to True"
                logger.info(pstr)
        valid_ids = []
        texts = []
        metadatas = []
        for id, doc in zip(ids, documents_):
            if id not in remove_ids:
                valid_ids.append(id)
                texts.append(doc.page_content)
                metadatas.append(doc.metadata)

        kwargs["ids"] = valid_ids
        return self.add_texts(texts, metadatas, **kwargs)

    def get_by_ids(self, ids: Sequence[str], /) -> list[Document]:
        """Get selected documents from vector store by their IDs.

        Args:
            ids: List of ids to retrieve.

        Returns:
            documents: List of Document objects found in the vector store.
        """

        collection_name = self.collection_name
        all_constraints = []
        for id in ids:
            constraints = {
                LANGCHAIN_ID_PROPERTY: ["==", str(id)],
            }
            all_constraints.append(constraints)

        results = {"list": self.utils.get_properties(collection_name)}

        docs = []
        for constraint in all_constraints:
            query = self.utils.add_descriptor(
                "FindDescriptor",
                collection_name,
                label=None,
                ref=None,
                props=None,
                link=None,
                k_neighbors=None,
                constraints=constraint,
                results=results,
            )

            response, _ = self.utils.run_vdms_query([query])

            if "FindDescriptor" in response[0]:
                this_docs = [
                    self.descriptor2document(doc)
                    for doc in response[0]["FindDescriptor"].get("entities", [])
                ]
                docs.extend(this_docs)
        return docs

    @property
    def embeddings(self) -> Embeddings:
        return self.embedding

    """ BACKWARD COMPATIBLE FUNCS """

    @classmethod
    def from_documents(
        cls: Type[VDMS],
        documents: list[Document],
        embedding: Embeddings,
        ids: Optional[list[str]] = None,
        collection_name: str = DEFAULT_COLLECTION_NAME,  # Add this line
        **kwargs: Any,
    ) -> VDMS:
        """Creates a new vector store from a list of documents

        Args:
            documents: List of documents
            embedding: Embedding function to use.
            ids: Optional list of IDs associated with the documents.
            collection_name (str): Name of the collection to create.
            kwargs: Additional keyword arguments.

        Returns:
            VectorStore: VectorStore initialized from documents and embeddings.
        """
        client: vdms.vdms = kwargs.pop("client")

        if "batch_size" not in kwargs:
            kwargs["batch_size"] = DEFAULT_INSERT_BATCH_SIZE

        vectorstore = cls(
            client=client,
            embedding=embedding,
            collection_name=collection_name,
            **kwargs,
        )
        vectorstore.add_documents(documents, ids=ids, **kwargs)
        return vectorstore

    def update_document(
        self, document_id: str, document: Document, **kwargs: Any
    ) -> None:
        """Update a document in the collection.

        Args:
            document_id (str): ID of the document to update.
            document (Document): Document to update.
        """
        self.update_documents([document_id], [document], **kwargs)

    def update_documents(
        self,
        ids: list[str],
        documents: list[Document],
        **kwargs: Any,
    ) -> None:
        """Update a document in the collection.

        Args:
            ids (list[str]): List of ids of the document to update.
            documents (list[Document]): List of documents to update.
        """
        kwargs["delete_existing"] = True
        self.add_documents(documents=documents, ids=ids, **kwargs)
        # collection_name = kwargs.pop("collection_name", cls.collection_name)
        # if collection_name == cls.collection_name:
        #     cls.add_documents(documents=documents, ids=ids, **kwargs)
        # else:
        #     vectorstore = cls(
        #         client=cls._client,
        #         embedding=cls.embedding,
        #         collection_name=collection_name,
        #         **kwargs,
        #     )
        #     vectorstore.add_documents(documents, ids=ids, **kwargs)

    def max_marginal_relevance_search_by_vector_with_score(
        self,
        embedding: list[float],
        k: int = DEFAULT_K,
        fetch_k: int = DEFAULT_FETCH_K,
        lambda_mult: float = 0.5,
        filter: Optional[dict[str, list]] = None,
        **kwargs: Any,
    ) -> list[tuple[Document, float]]:
        """Return docs selected using the maximal marginal relevance.

        Maximal marginal relevance optimizes for similarity to query AND diversity
        among selected documents.

        Args:
            embedding: Embedding vector to search for.
            k: Number of Documents to return.
            fetch_k: Number of Documents to fetch to pass to MMR algorithm.
            lambda_mult: Number between 0 and 1 that determines the degree
                        of diversity among the results with 0 corresponding
                        to maximum diversity and 1 to minimum diversity.
                        Defaults to 0.5.
            filter (Optional[dict[str, str]]): Filter by metadata. Defaults to None.

        Returns:
            List of Documents selected by maximal marginal relevance.
        """
        start_time = time.time()
        results = self.query_by_embeddings(
            query_embeddings=[embedding],
            k=k,
            fetch_k=fetch_k,
            filter=filter,
            include=["metadatas", "documents", "distances", "embeddings"],
        )

        if len(results[0][1]) == 0:
            # No results returned
            return []
        else:
            embedding_list = [
                list(self.utils.bytes2embedding(result)) for result in results[0][1]
            ]

            mmr_selected = maximal_marginal_relevance(
                np.array(embedding, dtype=np.float32),
                embedding_list,
                k=k,
                lambda_mult=lambda_mult,
            )

            logger.info(
                f"VDMS similarity search mmr took {time.time() - start_time:0.4f} secs"
            )
            candidates = self.results2documents_and_scores(results)
            return [(r, s) for i, (r, s) in enumerate(candidates) if i in mmr_selected]

    def max_marginal_relevance_search_with_score(
        self,
        query: str,
        k: int = DEFAULT_K,
        fetch_k: int = DEFAULT_FETCH_K,
        lambda_mult: float = 0.5,
        filter: Optional[dict[str, List]] = None,
        **kwargs: Any,
    ) -> List[Tuple[Document, float]]:
        """Returns similar documents to the query that also have diversity

        This algorithm balances relevance and diversity in the search results.

        Args:
            query: Text to look up documents similar to.
            k: Number of Documents to return. Defaults to 4.
            fetch_k: Number of Documents to fetch to pass to MMR algorithm.
            lambda_mult: Number between 0 and 1 that determines the degree
                of diversity among the results with 0 corresponding
                to maximum diversity and 1 to minimum diversity.
                Defaults to 0.5.
            **kwargs: Arguments to pass to the search method.

        Returns:
            List of Document objects ordered by decreasing similarity/diversty.
        """
        query_embedding = self.get_embedding_from_query(query)
        return self.max_marginal_relevance_search_by_vector_with_score(
            query_embedding, k, fetch_k, lambda_mult, filter, **kwargs
        )

    def similarity_search_with_score_by_vector(
        self,
        embedding: List[float],
        k: int = DEFAULT_K,
        fetch_k: int = DEFAULT_FETCH_K,
        filter: Optional[dict[str, List]] = None,
        **kwargs: Any,
    ) -> List[Tuple[Document, float]]:
        """
        Return docs most similar to embedding vector and similarity score.

        Args:
            embedding (List[float]): Embedding to look up documents similar to.
            k (int): Number of Documents to return. Defaults to 3.
            fetch_k (int): Number of candidates to fetch for knn (>= k).
            filter (Optional[Dict[str, str]]): Filter by metadata. Defaults to None.

        Returns:
            List[Tuple[Document, float]]: List of documents most similar to
            the query text. Lower score represents more similarity.
        """

        # kwargs["normalize_distance"] = True

        results = self.query_by_embeddings(
            query_embeddings=[embedding],
            n_results=k,
            fetch_k=fetch_k,
            filter=filter,
            **kwargs,
        )
        return self.results2documents_and_scores(results)

    def similarity_search_with_relevance_scores(
        self,
        query: str,
        k: int = DEFAULT_K,
        fetch_k: int = DEFAULT_FETCH_K,
        filter: Optional[dict[str, Any]] = None,
        **kwargs: Any,
    ) -> List[Tuple[Document, float]]:
        """Return docs and their similarity scores on a scale from 0 to 1.

        0 is dissimilar, 1 is most similar.

        Args:
            query: Input text.
            k (int): Number of Documents to return. Defaults to 3.
            fetch_k (int): Number of candidates to fetch for knn (>= k).
            filter (Optional[Dict[str, str]]): Filter by metadata. Defaults to None.
            **kwargs: kwargs to be passed to similarity search. Should include:
                score_threshold: Optional, a floating point value between 0 to 1 to
                    filter the resulting set of retrieved docs.

        Returns:
            List of Tuples of (doc, similarity_score).
        """
        score_threshold = kwargs.pop("score_threshold", None)

        if self.override_relevance_score_fn is None:
            # Scales 0 to 1
            kwargs["normalize_distance"] = True

        docs_and_scores = self.similarity_search_with_score(
            query=query,
            k=k,
            fetch_k=fetch_k,
            filter=filter,
            **kwargs,
        )

        if any(
            similarity < 0.0 or similarity > 1.0 for _, similarity in docs_and_scores
        ):
            logger.warning(
                f"Relevance scores must be between 0 and 1, got {docs_and_scores}",
                stacklevel=2,
            )

        docs_and_rel_scores: List[Any] = []
        for doc, score in docs_and_scores:
            if self.override_relevance_score_fn is None:
                # Lower is better to Higher is better
                similarity = 1.0 - score
            else:
                similarity = self.override_relevance_score_fn(score)

            if (score_threshold is None) or (
                score_threshold is not None and similarity >= score_threshold
            ):
                docs_and_rel_scores.append((doc, similarity))

        if len(docs_and_rel_scores) == 0:
            e_msg = "No relevant docs were retrieved using the relevance score"
            if score_threshold is not None:
                e_msg += f" threshold {score_threshold}"
            logger.warning(e_msg)
        return docs_and_rel_scores

    """ IMAGES """

    def add_images(
        self,
        uris: list[str],
        metadatas: Optional[list[dict]] = None,
        ids: Optional[list[str]] = None,
        add_path: Optional[bool] = True,
        **kwargs: Any,
    ) -> list[str]:
        """Run images through the embeddings and add to the vector store.

        Images are added as embeddings (AddDescriptor) instead of separate
        entity (AddImage) within VDMS to leverage similarity search capability

        Args:
            uris: List of paths to the images to add to the vector store.
            metadatas: Optional list of metadatas associated with the images.
            ids: Optional list of unique IDs.
            batch_size (int): Number of concurrent requests to send to the server.
            add_path: Bool to add image path as metadata

        Returns:
            List of ids from adding images into the vector store.
        """
        # Map from uris to blobs to base64
        b64_texts = [self.encode_image(image_path=uri) for uri in uris]

        if add_path and metadatas:
            for midx, uri in enumerate(uris):
                metadatas[midx]["image_path"] = uri
        elif add_path:
            metadatas = []
            for uri in uris:
                metadatas.append({"image_path": uri})

        # Set embeddings
        embeddings = self.embed_image(uris=uris)

        inserted_ids = self.add_embeddings(
            texts=b64_texts,
            embeddings=embeddings,
            metadatas=metadatas,
            ids=ids,
            **kwargs,
        )
        return inserted_ids

    """ VIDEOS """

    def add_videos(
        self,
        paths: list[str],
        texts: Optional[list[str]] = None,
        metadatas: Optional[list[dict]] = None,
        ids: Optional[list[str]] = None,
        add_path: Optional[bool] = True,
        **kwargs: Any,
    ) -> list[str]:
        """Run videos through the embeddings and add to the vector store.

        Videos are added as embeddings (AddDescriptor) instead of separate
        entity (AddVideo) within VDMS to leverage similarity search capability

        Args:
            paths: List of paths to the videos to add to the vector store.
            text: Optional list of text associated with the videos.
            metadatas: Optional list of metadatas associated with the videos.
            ids: Optional list of unique IDs.
            add_path: Bool to add video path as metadata

        Returns:
            List of ids from adding videos into the vector store.
        """
        if texts is None:
            texts = ["" for _ in paths]

        if add_path and metadatas:
            for midx, path in enumerate(paths):
                metadatas[midx]["video_path"] = path
        elif add_path:
            metadatas = []
            for path in paths:
                metadatas.append({"video_path": path})

        # Set embeddings
        embeddings = self.embed_video(paths=paths, **kwargs)

        inserted_ids = self.add_embeddings(
            texts=texts,
            embeddings=embeddings,
            metadatas=metadatas,
            ids=ids,
            **kwargs,
        )
        return inserted_ids

    """ EMBEDDING FUNCS """

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        if isinstance(self.embedding, Embeddings):
            return self.embedding.embed_documents(texts)
        else:
            p_str = "Must provide `embedding` which is expected"
            p_str += " to be an Embeddings object"
            raise ValueError(p_str)

    def embed_video(self, paths: List[str], **kwargs: Any) -> List[List[float]]:
        if self.embedding is not None and hasattr(self.embedding, "embed_video"):
            return self.embedding.embed_video(paths=paths, **kwargs)
        else:
            raise ValueError(
                "Must provide `embedding` which has attribute `embed_video`"
            )

    def embed_image(self, uris: List[str]) -> List[List[float]]:
        if self.embedding is not None and hasattr(self.embedding, "embed_image"):
            return self.embedding.embed_image(uris=uris)
        else:
            raise ValueError(
                "Must provide `embedding` which has attribute `embed_image`"
            )

    def embed_query(self, text: str) -> List[float]:
        if isinstance(self.embedding, Embeddings):
            return self.embedding.embed_query(text)
        else:
            raise ValueError(
                "Must provide `embedding` which is expected"
                " to be an Embeddings object"
            )

    def add_embeddings(
        self,
        texts: list[str],
        embeddings: list[list[float]],
        metadatas: Optional[list[dict]] = None,
        **kwargs: Any,
    ) -> list[str]:
        if metadatas is None:
            metadatas = [{} for _ in texts]
        else:
            metadatas = [self.utils.validate_vdms_properties(m) for m in metadatas]

        # Populate IDs
        ids = kwargs.pop("ids", None)
        if ids is None:
            ids = []
            for meta in metadatas:
                if LANGCHAIN_ID_PROPERTY in meta:
                    ids.append(meta[LANGCHAIN_ID_PROPERTY])
                else:
                    ids.append(str(uuid.uuid4()))

        if "batch_size" not in kwargs:
            kwargs["batch_size"] = DEFAULT_INSERT_BATCH_SIZE

        return self.add_from(
            texts=texts,
            embeddings=embeddings,
            ids=ids,
            metadatas=metadatas,
            **kwargs,
        )
        # return inserted_ids

    def get_embedding_from_query(self, query: str) -> list[float]:
        if not os_path.isfile(query) and hasattr(self.embedding, "embed_query"):
            query_embedding: list[float] = self.embed_query(query)
        elif os_path.isfile(query) and hasattr(self.embedding, "embed_image"):
            query_embedding = self.embed_image(uris=[query])[0]
        elif os_path.isfile(query) and hasattr(self.embedding, "embed_video"):
            query_embedding = self.embed_video(paths=[query])[0]
        else:
            error_msg = f"Could not generate embedding for query '{query}'."
            error_msg += "If using path for image or video, verify embedding model "
            error_msg += "has callable functions 'embed_image' or 'embed_video'."
            raise ValueError(error_msg)
        return query_embedding

    def encode_image(self, image_path: str) -> str:
        with open(image_path, "rb") as f:
            blob = f.read()
            return base64.b64encode(blob).decode("utf-8")

    def decode_image(self, base64_image: str) -> bytes:
        return base64.b64decode(base64_image)

    """ SEARCH """

    def query_by_embeddings(
        self,
        query_embeddings: Optional[list[list[float]]] = None,
        collection_name: Optional[str] = None,
        k: int = DEFAULT_K,
        fetch_k: int = DEFAULT_FETCH_K,
        filter: Union[None, dict[str, Any]] = None,
        results: Union[None, dict[str, Any]] = None,
        normalize_distance: bool = False,
        **kwargs: Any,
    ) -> list:
        self.check_and_update_properties()

        all_responses: list[Any] = []

        if collection_name is None:
            collection_name = self.collection_name

        if query_embeddings is None:
            return all_responses

        include = kwargs.get("include", ["metadatas"])
        if results is None and "metadatas" in include:
            results = {
                "list": self.collection_properties,
                "blob": "embeddings" in include,
            }

        for qemb in query_embeddings:
            response: list[dict] = []
            response_array: list[bytes] = []
            if fetch_k >= k:
                response, response_array = self.utils.get_descriptor_response(
                    "FindDescriptor",
                    collection_name,
                    k_neighbors=k,
                    fetch_k=fetch_k,
                    constraints=filter,
                    results=results,
                    normalize_distance=normalize_distance,
                    query_embedding=qemb,
                )

            try:
                num_returned = (
                    len(response[0]["FindDescriptor"].get("entities", []))
                    if "FindDescriptor" in response[0]
                    else 0
                )
                if num_returned != k:
                    logger.info(
                        f"Only {num_returned} returned. "
                        "Provide fetch_k > k ({k}); Currently set at {fetch_k}"
                    )
                result_entities = response[0]["FindDescriptor"].get("entities", [])
            except Exception as e:
                result_entities = []
                logger.info(f"Exception[in query_by_embeddings]: {e}")
                logger.info("Returning 0 results")
            for eidx, ent in enumerate(result_entities):
                response[0]["FindDescriptor"]["entities"][eidx] = {
                    k: v for k, v in ent.items() if v not in INVALID_METADATA_VALUE
                }

            all_responses.append([response, response_array])

        return all_responses

    def results2documents(self, results: Any) -> list[Document]:
        return [doc for doc, _ in self.results2documents_and_scores(results)]

    def results2documents_and_scores(
        self, results: Any
    ) -> list[Tuple[Document, float]]:
        final_res: list[Any] = []
        try:
            responses, blobs = results[0]
            descriptor_entities = responses[0]["FindDescriptor"].get("entities", [])
            if len(descriptor_entities) > 0:
                for ent in descriptor_entities:
                    distance = round(ent["_distance"], 10)
                    txt_contents = ent[TEXT_PROPERTY]
                    props = {
                        mkey: mval
                        for mkey, mval in ent.items()
                        if (
                            mval not in INVALID_METADATA_VALUE
                            and mkey not in INVALID_DOC_METADATA_KEYS
                        )
                    }
                    if LANGCHAIN_ID_PROPERTY in props:
                        d_id = props.pop(LANGCHAIN_ID_PROPERTY)

                    final_res.append(
                        (
                            Document(
                                page_content=txt_contents, metadata=props, id=d_id
                            ),
                            distance,
                        )
                    )
        except Exception as e:
            logger.warning(f"No results returned. Error while parsing results: {e}")
        return final_res

    def descriptor2document(self, descriptor_entity: dict) -> Document:
        metadata = {}
        d_id = None
        txt_contents = None
        for k, v in descriptor_entity.items():
            if k not in INVALID_DOC_METADATA_KEYS and v not in INVALID_METADATA_VALUE:
                metadata[k] = v
        if LANGCHAIN_ID_PROPERTY in metadata:
            d_id = metadata.pop(LANGCHAIN_ID_PROPERTY)
        # if TEXT_PROPERTY in d:
        txt_contents = descriptor_entity[TEXT_PROPERTY]
        doc = Document(page_content=txt_contents, metadata=metadata, id=d_id)
        return doc

    def count(self, **kwargs: Any) -> int:
        collection_name: str = kwargs.pop("collection_name", self.collection_name)
        all_queries: List[Any] = []

        results = {"count": "", "list": self.collection_properties}
        query = self.utils.add_descriptor(
            "FindDescriptor",
            collection_name,
            label=None,
            ref=None,
            props=None,
            link=None,
            k_neighbors=None,
            constraints=None,
            results=results,
        )

        all_queries.append(query)

        response, _ = self.utils.run_vdms_query(all_queries)
        try:
            return response[0]["FindDescriptor"]["returned"]
        except Exception as e:
            logger.info(f"Exception[in count]: {e}")
            logger.info("Count returning 0")
            return 0

    def get_by_constraints(
        self,
        collection_name: str,
        constraints: Optional[dict] = None,
        limit: Optional[int] = None,
        include: list[str] = ["metadata"],
    ) -> tuple[list, list]:
        """Get documents from vector store by constraints.
        Get embeddings and their associated data from the data store.
        If no constraints provided returns all embeddings up to limit.

        Args:
            constraints: A dict used to filter results by.
                   E.g. `{"color" : ["==", "red"], "price": [">", 4.00]}`. Optional.
            limit: The number of documents to return. Optional.
            include: A list of what to include in the results.
                     Can contain `"embeddings"`, `"metadatas"`, `"documents"`.
                     Ids are always included.
                     Defaults to `["metadatas", "documents"]`. Optional.
        """
        all_queries: list[Any] = []
        all_blobs: list[Any] = []

        results: dict[str, Any] = {"count": ""}

        if limit is not None:
            results["limit"] = limit

        # Include metadata
        if "metadata" in include:
            results["list"] = self.utils.get_properties(collection_name)

        # Include embedding
        if "embeddings" in include:
            results["blob"] = True

        query = self.utils.add_descriptor(
            "FindDescriptor",
            collection_name,
            k_neighbors=None,
            constraints=constraints,
            results=results,
        )

        all_queries.append(query)

        response, resp_arr_bytes = self.utils.run_vdms_query(all_queries, all_blobs)

        try:
            # if "FindDescriptor" in response[0]:
            this_docs = [
                self.descriptor2document(doc)
                for doc in response[0]["FindDescriptor"].get("entities", [])
            ]
            embedding_list = [
                list(self.utils.bytes2embedding(blob)) for blob in resp_arr_bytes
            ]
            return this_docs, embedding_list
        except Exception as e:
            logger.info(f"Exception[in get_by_constraints]: {e}")
            logger.info("Returning []")
            return [], []

    """ OTHER FUNCS """

    def _check_required_inputs(
        self,
        collection_name: str,
        embedding_dimensions: Union[int, None],
        **kwargs: Any,
    ) -> None:
        # Check Distance Metric
        if self.distance_strategy not in AVAILABLE_DISTANCE_METRICS:
            raise ValueError("distance_strategy must be either 'L2' or 'IP'")

        # Check Engines
        if self.similarity_search_engine not in AVAILABLE_ENGINES:
            raise ValueError(
                "engine must be either 'TileDBDense', 'TileDBSparse', "
                + "'FaissFlat', 'FaissIVFFlat', 'FaissHNSWFlat', or 'Flinng'"
            )

        # Check Embedding Func is provided and store dimension size
        if self.embedding is None:
            raise ValueError("Must provide embedding function")

        if embedding_dimensions is not None:
            self.embedding_dimension = embedding_dimensions
        elif self.embedding is not None and hasattr(self.embedding, "embed_query"):
            self.embedding_dimension = len(
                self.embedding.embed_query("This is a sample sentence.")
            )
        elif self.embedding is not None and (
            hasattr(self.embedding, "embed_image")
            or hasattr(self.embedding, "embed_video")
        ):
            dim_err_str = "Please define embedding_dimensions"
            if hasattr(self.embedding, "model"):
                try:
                    self.embedding_dimension = (
                        self.embedding.model.token_embedding.embedding_dim
                    )
                except ValueError:
                    raise ValueError(dim_err_str)
            else:
                raise ValueError(dim_err_str)

        # Check for properties
        current_props = self.utils.get_properties(collection_name)
        if hasattr(self, "collection_properties"):
            missing_elements = list(
                set(current_props) - set(self.collection_properties)
            )  # element in current not in props
            if len(missing_elements) > 0:
                self.collection_properties.extend(missing_elements)
        else:
            self.collection_properties: list[str] = current_props

        self.collection_properties.sort()

    def _create_collection(self) -> None:
        collection_name = self.collection_name
        embedding_dimension = self.embedding_dimension
        engine = self.similarity_search_engine
        metric = self.distance_strategy

        query = self.utils.add_descriptor_set(
            "AddDescriptorSet",
            collection_name,
            embedding_dimension,
            engine=getattr(engine, "value", engine),
            metric=getattr(metric, "value", metric),
        )

        response, _ = self.utils.run_vdms_query([query])

        if "FailedCommand" in response[0]:
            raise ValueError(f"Failed to add collection {collection_name}")

        if response[0]["AddDescriptorSet"]["status"] == 0:
            status = "created"
        else:
            status = "exists"

        logger.info(f"Descriptor set {collection_name} {status}")

    def _len_check_if_sized(self, x: Any, y: Any, x_name: str, y_name: str) -> None:
        """
        Check that sizes of two variables are the same

        Args:
            x: Variable to compare
            y: Variable to compare
            x_name: Name for variable x
            y_name: Name for variable y
        """
        if isinstance(x, Sized) and isinstance(y, Sized) and len(x) != len(y):
            raise ValueError(
                f"{x_name} and {y_name} expected to be equal length but "
                f"len({x_name})={len(x)} and len({y_name})={len(y)}"
            )
        return

    def check_and_update_properties(self) -> None:
        if self.updated_properties_flag:
            pushed_props = self.utils.get_properties(self.collection_name)
            self.collection_properties.sort()
            pushed_props.sort()
            if self.collection_properties != pushed_props:
                self.collection_properties = pushed_props
            self.updated_properties_flag = False

    def add_from(
        self,
        texts: list[str],
        embeddings: list[list[float]],
        ids: list[str],
        metadatas: Optional[list[dict]] = None,
        **kwargs: Any,
    ) -> list[str]:
        # Get initial properties
        inserted_ids: list[str] = []
        batch_size = int(kwargs.get("batch_size", DEFAULT_INSERT_BATCH_SIZE))
        total_count = len(texts)

        for start_idx in range(0, total_count, batch_size):
            end_idx = min(start_idx + batch_size, total_count)

            batch_texts = texts[start_idx:end_idx]
            batch_embedding_vectors = embeddings[start_idx:end_idx]
            batch_ids = ids[start_idx:end_idx]

            if metadatas:
                batch_metadatas = metadatas[start_idx:end_idx]

            try:
                result_ids = self.add_batch(
                    self.collection_name,
                    embeddings=batch_embedding_vectors,
                    texts=batch_texts,
                    metadatas=batch_metadatas,
                    ids=batch_ids,
                )

                inserted_ids.extend(result_ids)
            except Exception as e:
                logger.error(
                    "Failed to insert batch starting at entity: %s-%s",
                    start_idx,
                    end_idx - 1,
                )
                raise e

        # Update Properties
        self.push_update_properties(
            self.collection_name,
        )
        return inserted_ids

    def push_update_properties(
        self,
        collection_name: str,
    ) -> None:
        pushed_props = self.utils.get_properties(collection_name)
        missing_elements = list(
            set(self.collection_properties) - set(pushed_props)
        )  # element in current not in props
        if len(missing_elements) > 0:
            pushed_props.extend(missing_elements)
            pushed_props.sort()  # perform remaining if list updated

            all_queries, blob_arr = self.utils.build_property_query(
                collection_name,
                command_type="update",
                all_properties=pushed_props,
            )
            response, _ = self.utils.run_vdms_query(all_queries, [blob_arr])
            self.updated_properties_flag = True

    def add_batch(
        self,
        collection_name: str,
        texts: list[str],
        embeddings: list[list[float]],
        metadatas: Optional[list[dict]] = None,
        ids: Optional[list[str]] = None,
    ) -> list:
        self._len_check_if_sized(texts, embeddings, "texts", "embeddings")

        metadatas = metadatas if metadatas is not None else [{} for _ in texts]
        self._len_check_if_sized(texts, metadatas, "texts", "metadatas")

        ids = ids if ids is not None else [str(uuid.uuid4()) for _ in texts]
        self._len_check_if_sized(texts, ids, "texts", "ids")

        extended_emb: list[Any] = []
        batch_properties: list[dict] = []
        for meta, emb, doc, id in zip(metadatas, embeddings, texts, ids):
            extended_emb.extend(emb)
            batch_properties.append(self.get_props_from_metadata(doc, meta, id))
        all_blobs = [self.utils.embedding2bytes(extended_emb)]
        all_queries = [
            self.utils.add_descriptor(
                "AddDescriptor",
                collection_name,
                label=None,
                ref=None,
                props=batch_properties,
                link=None,
                k_neighbors=None,
                constraints=None,
                results=None,
            )
        ]

        response, _ = self.utils.run_vdms_query(all_queries, all_blobs)

        try:
            return ids if response[0]["AddDescriptor"]["status"] == 0 else []
        except Exception as e:
            if "OutOfJournalSpace" in response[0]["info"]:
                try:
                    logger.info("OutOfJournalSpace: Splitting batch in half")
                    old_batch = len(all_queries)
                    new_batch_size = old_batch // 2
                    emb_len = len(emb)
                    for start_idx in range(0, old_batch, new_batch_size):
                        end_idx_blob = min(
                            start_idx * emb_len + new_batch_size - 1, len(all_blobs)
                        )
                        blobs = all_blobs[start_idx * emb_len : end_idx_blob]

                        end_idx = min(start_idx + new_batch_size - 1, old_batch)
                        queries = all_queries[start_idx:end_idx]
                        response, _ = self.utils.run_vdms_query(queries, blobs)
                except Exception:
                    raise ValueError(f"Lower batch_size to < {old_batch} and rerun")

                return ids if response[0]["AddDescriptor"]["status"] == 0 else []
            else:
                logger.info(f"Exception[in add_batch]: {e}")
                logger.info("Returning []")
                return []

    def get_props_from_metadata(
        self,
        # collection_name: str,
        document: str,
        metadata: Optional[dict] = None,
        id: Optional[str] = None,
    ) -> dict[str, Any]:
        if id is None:
            props = {}
        else:
            props = {LANGCHAIN_ID_PROPERTY: str(id)}

        if metadata:
            for k, v in metadata.items():
                if k not in props:
                    props[k] = v
            # props.update(metadata)
            if LANGCHAIN_ID_PROPERTY not in props and "id" in metadata:
                props[LANGCHAIN_ID_PROPERTY] = str(metadata["id"])
        if document not in [None, ""]:
            props["content"] = document

        for k in props.keys():
            if k not in self.collection_properties:
                self.collection_properties.append(k)
        self.collection_properties.sort()
        return props

    def batch_delete(
        self,
        collection_name: str,
        constraints: dict,
        ids: Optional[list[str]] = None,
        batch_size: int = DEFAULT_INSERT_BATCH_SIZE,
    ) -> list:
        resp_dict: dict = {}
        resp_dict.setdefault(
            "FindDescriptor", {"entities": list(), "returned": 0, "status": 0}
        )
        new_response = [resp_dict]

        if ids is None:
            # return new_response
            # FIND ALL
            query = self.utils.add_descriptor(
                "FindDescriptor",
                collection_name,
                label=None,
                ref=None,
                props=None,
                link=None,
                k_neighbors=None,
                constraints=constraints,
                results={"list": self.collection_properties},
            )
            response, _ = self.utils.run_vdms_query([query])

            for res in response:
                if "FindDescriptor" in res:
                    new_response[0]["FindDescriptor"]["entities"].extend(
                        res["FindDescriptor"]["entities"]
                    )
        else:
            for start_idx in range(0, len(ids), batch_size):
                end_idx = min(start_idx + batch_size, len(ids))
                batch_ids = ids[start_idx:end_idx]

                all_queries = []
                for i in batch_ids:
                    tmp_ = {LANGCHAIN_ID_PROPERTY: ["==", i]}
                    if constraints is not None:
                        tmp_.update(constraints)

                    query = self.utils.add_descriptor(
                        "FindDescriptor",
                        collection_name,
                        label=None,
                        ref=None,
                        props=None,
                        link=None,
                        k_neighbors=None,
                        constraints=tmp_,
                        results={"list": self.collection_properties},
                    )

                    all_queries.append(query)

                if all_queries == []:
                    return new_response

                response, _ = self.utils.run_vdms_query(all_queries)

                for res in response:
                    if "FindDescriptor" in res:
                        new_response[0]["FindDescriptor"]["entities"].extend(
                            res["FindDescriptor"]["entities"]
                        )

        new_response[0]["FindDescriptor"]["returned"] = len(
            new_response[0]["FindDescriptor"]["entities"]
        )
        return new_response


# Vectorstore Client Connector
def VDMS_Client(
    host: str = "localhost",
    port: int = 55555,
) -> vdms.vdms:
    """VDMS client for the VDMS server.

    Args:
        host: IP or hostname of VDMS server
        port: Port to connect to VDMS server
    """
    try:
        import vdms
    except ImportError:
        raise ImportError(
            "Could not import vdms python package. "
            "Please install it with `pip install vdms."
        )

    client = vdms.vdms()
    client.connect(host, port)
    return client


# Useful VDMS Functions
class VDMS_Utils:
    def __init__(self, client: vdms.vdms) -> None:
        self.client = client

        # Check connection to client
        if not self.client.is_connected():
            raise ValueError(
                "VDMS client must be connected to a VDMS server."
                + "Please use VDMS_Client to establish a connection"
            )

    def add_descriptor(
        self,
        command_str: str,
        setname: str,
        label: Optional[str] = None,
        ref: Optional[int] = None,
        props: Optional[Union[dict, list]] = None,
        link: Optional[dict] = None,
        k_neighbors: Optional[int] = None,
        constraints: Optional[dict] = None,
        results: Optional[dict] = None,
    ) -> dict[str, dict[str, Any]]:
        entity: dict[str, Any] = {"set": setname}

        if "Add" in command_str and label:
            entity["label"] = label

        if ref is not None:
            entity["_ref"] = ref

        if isinstance(props, list) and len(props) > 1:
            entity["batch_properties"] = props
        elif (
            isinstance(props, list)
            and len(props) == 1
            and props[0] not in INVALID_METADATA_VALUE
        ):
            entity["properties"] = props[0]
        elif isinstance(props, dict) and props not in INVALID_METADATA_VALUE:
            entity["properties"] = props

        if "Add" in command_str and link is not None:
            entity["link"] = link

        if "Find" in command_str and k_neighbors is not None:
            entity["k_neighbors"] = int(k_neighbors)

        if "Find" in command_str and constraints not in INVALID_METADATA_VALUE:
            entity["constraints"] = constraints

        if "Find" in command_str and results not in INVALID_METADATA_VALUE:
            entity["results"] = results

        query = {command_str: entity}
        return query

    def add_descriptor_set(
        self,
        command_str: str,
        name: str,
        num_dims: Optional[int] = None,
        engine: Optional[str] = None,
        metric: Optional[str] = None,
        ref: Optional[int] = None,
        props: Optional[dict] = None,
        link: Optional[dict] = None,
        storeIndex: bool = False,
        constraints: Optional[dict] = None,
        results: Optional[dict] = None,
    ) -> dict[str, Any]:
        if command_str == "AddDescriptorSet" and all(
            var is not None for var in [name, num_dims]
        ):
            entity: dict[str, Any] = {
                "name": name,
                "dimensions": num_dims,
            }

            if engine is not None:
                entity["engine"] = engine

            if metric is not None:
                entity["metric"] = metric

            if ref is not None:
                entity["_ref"] = ref

            if props not in [None, {}]:
                entity["properties"] = props

            if link is not None:
                entity["link"] = link

        elif command_str == "FindDescriptorSet":
            entity = {"set": name}

            if storeIndex:
                entity["storeIndex"] = storeIndex

            if constraints not in [None, {}]:
                entity["constraints"] = constraints

            if results is not None:
                entity["results"] = results

        else:
            raise ValueError(f"Unknown command: {command_str}")

        query = {command_str: entity}
        return query

    def add_entity_with_blob(
        self, collection_name: str, all_properties: list
    ) -> Tuple[dict[str, Any], bytes]:
        all_properties_str = ",".join(all_properties) if len(all_properties) > 0 else ""

        querytype = "AddEntity"
        entity: dict[str, Any] = {}
        entity["class"] = "properties"
        entity["blob"] = True  # New

        props: dict[str, Any] = {"name": collection_name}
        props["type"] = "queryable properties"
        props[TEXT_PROPERTY] = all_properties_str
        entity["properties"] = props

        byte_data = self.str2bytes(all_properties_str)

        query: dict[str, Any] = {}
        query[querytype] = entity
        return query, byte_data

    def build_property_query(
        self,
        collection_name: str,
        command_type: str = "find",
        all_properties: list = [],
        ref: Optional[int] = None,
    ) -> Tuple[Any, Any]:
        all_queries: list[Any] = []
        blob_arr: list[Any] = []

        choices = ["find", "add", "update"]
        if command_type.lower() not in choices:
            raise ValueError(
                "[!] Invalid type. Choices are : {}".format(",".join(choices))
            )

        if command_type.lower() == "find":
            query = self.find_property_entity(collection_name, unique_entity=True)
            all_queries.append(query)

        elif command_type.lower() == "add":
            query, byte_data = self.add_entity_with_blob(
                collection_name, all_properties
            )
            all_queries.append(query)
            blob_arr.append(byte_data)

        elif command_type.lower() == "update":
            # Find & Delete
            query = self.find_property_entity(collection_name, deletion=True)
            all_queries.append(query)

            # Add
            query, byte_data = self.add_entity_with_blob(
                collection_name, all_properties
            )
            all_queries.append(query)
            blob_arr.append(byte_data)

        return all_queries, blob_arr

    def bytes2embedding(self, blob: bytes) -> Any:
        emb = np.frombuffer(blob, dtype="float32")
        return emb

    def bytes2str(self, in_bytes: bytes) -> str:
        return in_bytes.decode()

    def check_valid_response(
        self, all_queries: list[dict], response: Any
    ) -> tuple[Any, bool]:
        cmd_list = self.get_cmds_from_query(all_queries)
        valid_res = isinstance(response, list) and any(
            cmd in response[0]
            and "returned" in response[0][cmd]
            and response[0][cmd]["returned"] > 0
            for cmd in cmd_list
        )

        # ID required
        for ridx, res in enumerate(response):
            if "FindDescriptor" in res:
                ent = res["FindDescriptor"].get("entities", [])
                response[ridx]["FindDescriptor"]["entities"] = [
                    e for e in ent if LANGCHAIN_ID_PROPERTY in e
                ]
                response[ridx]["FindDescriptor"]["returned"] = len(
                    response[ridx]["FindDescriptor"]["entities"]
                )

        return response, valid_res

    def embedding2bytes(
        self, embedding: Union[list[float], None]
    ) -> Union[bytes, None]:
        """Convert embedding to bytes."""

        blob = None
        if embedding is not None:
            emb = np.array(embedding, dtype="float32")
            blob = emb.tobytes()
        return blob

    def find_property_entity(
        self,
        collection_name: str,
        unique_entity: Optional[bool] = False,
        deletion: Optional[bool] = False,
    ) -> dict[str, dict[str, Any]]:
        querytype = "FindEntity"
        entity: dict[str, Any] = {}
        entity["class"] = "properties"
        if unique_entity:
            entity["unique"] = unique_entity

        results: dict[str, Any] = {}
        results["blob"] = True
        results["count"] = ""
        results["list"] = [TEXT_PROPERTY]
        entity["results"] = results

        constraints: dict[str, Any] = {}
        if deletion:
            constraints["_deletion"] = ["==", 1]
        constraints["name"] = ["==", collection_name]
        entity["constraints"] = constraints

        query: dict[str, Any] = {}
        query[querytype] = entity
        return query

    def get_cmds_from_query(self, all_queries: list) -> list[str]:
        return list(set([k for q in all_queries for k in q.keys()]))

    def get_descriptor_response(
        self,
        command_str: str,
        setname: str,
        k_neighbors: int = DEFAULT_K,
        fetch_k: int = DEFAULT_FETCH_K,
        constraints: Optional[dict] = None,
        results: Optional[dict[str, Any]] = None,
        query_embedding: Optional[list[float]] = None,
        normalize_distance: bool = False,
    ) -> Tuple[list[dict[str, Any]], list]:
        all_blobs: list[Any] = []
        if k_neighbors >= fetch_k:
            raise ValueError(f"Provide fetch_k > k; Currently set at {fetch_k}")
        blob = self.embedding2bytes(query_embedding)
        if blob is not None:
            all_blobs.append(blob)

        if constraints is None:
            # K results returned
            response, response_array = self.get_k_candidates(
                setname=setname,
                k=fetch_k,
                results=results,
                all_blobs=all_blobs,
            )

            if (
                len(response) > 0
                and command_str in response[0]
                and "entities" in response[0][command_str]
            ):
                new_entities = response[0][command_str]["entities"][:k_neighbors]
                response[0][command_str]["entities"] = new_entities
                response[0][command_str]["returned"] = len(new_entities)
                response_array = response_array[: len(new_entities)]
        else:
            if results is None:
                results = {"list": [LANGCHAIN_ID_PROPERTY]}
            elif "list" not in results:
                results["list"] = [LANGCHAIN_ID_PROPERTY]
            elif LANGCHAIN_ID_PROPERTY not in results["list"]:
                results["list"].append(LANGCHAIN_ID_PROPERTY)

            # (1) Find docs satisfy constraints
            query = self.add_descriptor(
                command_str,
                setname,
                constraints=constraints,
                results=results,
            )
            response, response_array = self.run_vdms_query([query])
            if command_str in response[0] and response[0][command_str]["returned"] > 0:
                ids_of_interest = [
                    ent[LANGCHAIN_ID_PROPERTY]
                    for ent in response[0][command_str]["entities"]
                ]
            else:
                return [], []

            # (2) Find top fetch_k results
            response, response_array = self.get_k_candidates(
                setname=setname,
                k=fetch_k,
                results=results,
                all_blobs=all_blobs,
            )
            if command_str not in response[0] or (
                command_str in response[0] and response[0][command_str]["returned"] == 0
            ):
                return [], []

            # (3) Intersection of (1) & (2) using ids
            new_entities = []
            for ent in response[0][command_str]["entities"]:
                if ent[LANGCHAIN_ID_PROPERTY] in ids_of_interest:
                    new_entities.append(ent)
                if len(new_entities) == k_neighbors:
                    break
            response[0][command_str]["entities"] = new_entities
            response[0][command_str]["returned"] = len(new_entities)
            response_array = response_array[: len(new_entities)]
            if len(new_entities) < k_neighbors:
                p_str = "Returned items < k_neighbors; Try increasing fetch_k"
                logger.warning(p_str)

        if normalize_distance:
            min_dist = 0.0
            max_dist = max(
                [
                    ent["_distance"]
                    for ent in response[0][command_str]["entities"]
                    if ent["_distance"] != np.inf
                ]
            )
            for ent_idx, ent in enumerate(response[0][command_str]["entities"]):
                try:
                    ent["_distance"] = (ent["_distance"] - min_dist) / (
                        max_dist - min_dist
                    )
                except Exception as e:
                    ent["_distance"] = 0.0 if max_dist == min_dist else 1.0
                    logger.info(f"Exception[in get_descriptor_response]: {e}")
                    logger.info("Normalizing to " + str(ent["_distance"]))
                response[0][command_str]["entities"][ent_idx]["_distance"] = ent[
                    "_distance"
                ]
        return response, response_array

    def get_k_candidates(
        self,
        setname: str,
        k: Optional[int] = None,
        results: Optional[dict[str, Any]] = None,
        all_blobs: Optional[list] = None,
    ) -> Tuple[list[dict[str, Any]], list]:
        command_str = "FindDescriptor"
        query = self.add_descriptor(
            command_str,
            setname,
            k_neighbors=k,
            results=results,
        )
        response, response_array = self.run_vdms_query([query], all_blobs)

        if "FailedCommand" in response[0]:
            return [], []

        return response, response_array

    def get_properties(
        self,
        collection_name: str,
        unique_entity: Optional[bool] = False,
        deletion: Optional[bool] = False,
    ) -> list[str]:
        find_query = self.find_property_entity(
            collection_name, unique_entity=unique_entity, deletion=deletion
        )
        response, response_blob = self.run_vdms_query([find_query])
        if len(response_blob) > 0:
            collection_properties = self.bytes2str(response_blob[0]).split(",")
        else:
            collection_properties = deepcopy(DEFAULT_PROPERTIES)
        collection_properties.sort()
        return collection_properties

    def run_vdms_query(
        self,
        all_queries: list[dict],
        all_blobs: Optional[list] = [],
        print_last_response: Optional[bool] = False,
    ) -> Tuple[Any, Any]:
        response, response_array = self.client.query(all_queries, all_blobs)

        response, _ = self.check_valid_response(all_queries, response)
        if print_last_response:
            self.client.print_last_response()
        return response, response_array

    def str2bytes(self, in_str: str) -> bytes:
        return str.encode(in_str)

    def validate_vdms_properties(self, metadata: dict[str, Any]) -> dict:
        new_metadata: dict[str, Any] = {}
        for key, value in metadata.items():
            if not isinstance(value, list):
                new_metadata[str(key)] = value
        return new_metadata
