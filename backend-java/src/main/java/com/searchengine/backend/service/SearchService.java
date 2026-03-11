package com.searchengine.backend.service;

import com.searchengine.backend.dto.PaperDto;
import com.searchengine.grpc.AutocompleteRequest;
import com.searchengine.grpc.AutocompleteResponse;
import com.searchengine.grpc.SearchRequest;
import com.searchengine.grpc.SearchResponse;
import com.searchengine.grpc.SearchServiceGrpc;
import com.searchengine.grpc.SimilarRequest;

import java.util.List;
import java.util.stream.Collectors;

import net.devh.boot.grpc.client.inject.GrpcClient;
import org.springframework.cache.annotation.Cacheable;
import org.springframework.stereotype.Service;

@Service
public class SearchService {

        @GrpcClient("search-service")
        private SearchServiceGrpc.SearchServiceBlockingStub searchServiceStub;

        // SEARCH
        @Cacheable(value = "search", key = "#query + '_' + #page")
        public List<PaperDto> searchPapers(String query, int page) {

                System.out.println("Cache miss for query: " + query + " page: " + page);

                SearchRequest request = SearchRequest.newBuilder()
                                .setQuery(query)
                                .setPage(page)
                                .setSize(10)
                                .build();

                SearchResponse response = searchServiceStub.search(request);

                return response.getPapersList()
                                .stream()
                                .map(p -> PaperDto.builder()
                                                .id(p.getId())
                                                .title(p.getTitle())
                                                .abstractText(p.getAbstract())
                                                .authors(p.getAuthorsList())
                                                .build())
                                .collect(Collectors.toList());
        }

        // SIMILAR PAPERS
        @Cacheable(value = "similar", key = "#paperId")
        public List<PaperDto> similarPapers(String paperId) {

                System.out.println("Cache miss for similar paper: " + paperId);

                SimilarRequest request = SimilarRequest.newBuilder()
                                .setPaperId(paperId)
                                .setTopK(10)
                                .build();

                SearchResponse response = searchServiceStub.similar(request);

                return response.getPapersList()
                                .stream()
                                .map(p -> PaperDto.builder()
                                                .id(p.getId())
                                                .title(p.getTitle())
                                                .abstractText(p.getAbstract())
                                                .authors(p.getAuthorsList())
                                                .build())
                                .collect(Collectors.toList());
        }

        // AUTOCOMPLETE
        @Cacheable(value = "autocomplete", key = "#prefix")
        public List<String> autocomplete(String prefix) {

                System.out.println("Cache miss for autocomplete: " + prefix);

                AutocompleteRequest request = AutocompleteRequest.newBuilder()
                                .setPrefix(prefix)
                                .build();

                AutocompleteResponse response = searchServiceStub.autocomplete(request);

                return response.getSuggestionsList();
        }
}