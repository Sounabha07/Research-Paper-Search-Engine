package com.searchengine.backend.dto;

import java.io.Serializable;
import java.util.List;

import lombok.AllArgsConstructor;
import lombok.Builder;
import lombok.Data;
import lombok.NoArgsConstructor;

@Data
@Builder
@NoArgsConstructor
@AllArgsConstructor
public class PaperDto implements Serializable {

    private String id;

    private String title;

    private String abstractText;

    private List<String> authors;

}