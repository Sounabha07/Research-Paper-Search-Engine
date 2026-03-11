package com.searchengine.backend.controller;

import com.searchengine.backend.dto.PaperDto;
import com.searchengine.backend.service.SearchService;
import java.util.Collections;
import java.util.List;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.CrossOrigin;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

@RestController
@CrossOrigin(origins = "*") // Allow frontend to fetch data
public class SearchController {

    private final SearchService searchService;

    @Autowired
    public SearchController(SearchService searchService) {
        this.searchService = searchService;
    }

    @GetMapping("/search")
    public ResponseEntity<List<PaperDto>> search(@RequestParam("q") String query) {
        if (query == null || query.trim().isEmpty()) {
            return ResponseEntity.ok(Collections.emptyList());
        }

        List<PaperDto> results = searchService.searchPapers(query);
        return ResponseEntity.ok(results);
    }

    @GetMapping("/similar/{paperId}")
    public ResponseEntity<List<PaperDto>> getSimilarPapers(@PathVariable String paperId) {
        if (paperId == null || paperId.trim().isEmpty()) {
            return ResponseEntity.ok(Collections.emptyList());
        }

        List<PaperDto> results = searchService.similarPapers(paperId);
        return ResponseEntity.ok(results);
    }

    @GetMapping("/autocomplete")
    public ResponseEntity<List<String>> getAutocomplete(@RequestParam("q") String prefix) {
        if (prefix == null || prefix.trim().isEmpty()) {
            return ResponseEntity.ok(Collections.emptyList());
        }

        List<String> suggestions = searchService.autocomplete(prefix);
        return ResponseEntity.ok(suggestions);
    }
}
